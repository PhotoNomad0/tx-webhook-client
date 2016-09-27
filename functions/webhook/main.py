# -*- coding: utf-8 -*-

#
# Webhook client for receiving a payload from Gogs to process the job
#

from __future__ import print_function

# Python libraries
import os
import tempfile
import boto3
import requests
import json

# Functions from Python libraries
from shutil import copyfile
from glob import glob
from datetime import datetime

# Functions from unfoldingWord libraries
from general_tools.file_utils import unzip, add_file_to_zip, make_dir, write_file
from general_tools.url_utils import download_file


def handle(event, context):
    # Getting data from payload which is the JSON that was sent from tx-manager
    if 'data' not in event:
        raise Exception('"data" not in payload')
    data = event['data']

    env_vars = {}
    if 'vars' in event and isinstance(event['vars'], dict):
        env_vars = event['vars']

    commit_id = data['after']
    commit = None
    for commit in data['commits']:
        if commit['id'] == commit_id:
            break

    commit_url = commit['url']
    commit_message = commit['message']

    if 'https://git.door43.org/' not in commit_url and 'http://test.door43.org:3000/' not in commit_url:
        raise Exception('Currently only git.door43.org repositories are supported.')

    pre_convert_bucket = env_vars['pre_convert_bucket']
    cdn_bucket = env_vars['cdn_bucket']
    gogs_user_token = env_vars['gogs_user_token']
    api_url = env_vars['api_url']

    repo_name = data['repository']['name']
    repo_owner = data['repository']['owner']['username']
    compare_url = data['compare_url']

    if 'pusher' in data:
        pusher = data['pusher']
    else:
        pusher = {'username': commit['author']['username']}
    pusher_username = pusher['username']

    # The following sections of code will:
    # 1) download and unzip repo files
    # 2) massage the repo files by creating a new directory and file structure
    # 3) zip up the massages filed
    # 4) upload massaged files to S3 in zip file

    # 1) Download and unzip the repo files
    repo_zip_url = commit_url.replace('commit', 'archive') + '.zip'
    repo_zip_file = os.path.join(tempfile.gettempdir(), repo_zip_url.rpartition('/')[2])
    repo_dir = tempfile.mkdtemp(prefix='repo_')
    try:
        print('Downloading {0}...'.format(repo_zip_url), end=' ')
        if not os.path.isfile(repo_zip_file):
            download_file(repo_zip_url, repo_zip_file)
    finally:
        print('finished.')

    # Unzip the archive
    try:
        print('Unzipping {0}...'.format(repo_zip_file), end=' ')
        unzip(repo_zip_file, repo_dir)
    finally:
        print('finished.')

    # 2) Massage the content to just be a directory of MD files in alphabetical order as they should be compiled together in the converter
    resource_type = None
    input_format = None
    source_dir = os.path.join(repo_dir, repo_name, 'content')
    output_dir = tempfile.mktemp(prefix='files_')
    manifest_filepath = os.path.join(repo_dir, repo_name, 'manifest.json')
    # Get info from manifest.json if there is one
    if os.path.isfile(manifest_filepath):
        with open(manifest_filepath) as f:
            manifest = json.load(f)
            if 'resource' in manifest and 'id' in manifest['resource']:
                resource_type = 'bible'
            elif 'source_translations' in manifest and 'resource_slug' in manifest['source_translations'][0]:
                resource_type = manifest['status'][0]['resource_slug']
            elif 'status' in manifest and 'source_translations' in manifest['status'] and 'resource_slug' in manifest['status']['source_translations'][0]:
                resource_type = manifest['status']['source_translations'][0]['resource_slug']
    if not resource_type:
        if '-' in repo_name:
            resource_type = repo_name.split('-')[:-1]
        elif '_' in repo_name:
            resource_type = repo_name.split('_')[:-1]
        else:
            resource_type = repo_name

    if resource_type == 'ulb' or resource_type == 'udb':
        resource_type = 'bible'

    # Handle Bible files
    if resource_type == 'bible':
        input_format = "usfm"
        files = glob(os.path.join(source_dir, '*.usfm'))
        make_dir(output_dir)
        print('Massaging content from {0} to {1}...'.format(source_dir, output_dir), end=' ')
        for filename in files:
            copyfile(filename, os.path.join(output_dir, os.path.basename(filename)))
        print('finished.')
    # Handle OBS files
    elif resource_type == 'obs':
        input_format = "md"
        source_dir = os.path.join(repo_dir, repo_name, 'content')
        files = glob(os.path.join(source_dir, '*.md'))
        output_dir = tempfile.mktemp(prefix='files_')
        make_dir(output_dir)
        print('Massaging content from {0} to {1}...'.format(source_dir, output_dir), end=' ')
        for filename in files:
            copyfile(filename, os.path.join(output_dir, os.path.basename(filename)))
        # Copy over front and back matter
        copyfile(os.path.join(source_dir, '_front', 'front-matter.md'),
                 os.path.join(output_dir, 'front-matter.md'))
        copyfile(os.path.join(source_dir, '_back', 'back-matter.md'),
                 os.path.join(output_dir, 'back-matter.md'))
        print('finished.')
    else:
        raise Exception("Bad Request: No resource type could be determined for repository {0}/{1}.".format(repo_owner, repo_name))

    # 3) Zip up the massaged files
    zip_filename = context.aws_request_id+'.zip' # context.aws_request_id is a unique ID for this lambda call, so using it to not conflict with other requests
    zip_filepath = os.path.join(tempfile.gettempdir(), zip_filename)
    files = glob(os.path.join(output_dir, '*'))
    print('Zipping files from {0} to {1}...'.format(output_dir, zip_filepath), end=' ')
    for filename in files:
        add_file_to_zip(zip_filepath, filename, os.path.basename(filename))
    if os.path.isfile(manifest_filepath):
        add_file_to_zip(zip_filepath, manifest_filepath, os.path.basename(manifest_filepath))
    print('finished.')

    # 4) Upload zipped file to the S3 bucket (you may want to do some try/catch and give an error if fails back to Gogs)
    print('Uploading {0} to {1}...'.format(zip_filepath, pre_convert_bucket), end=' ')
    s3_client = boto3.client('s3')
    file_key = "tx-webhook-client/"+zip_filename
    s3_client.upload_file(zip_filepath, pre_convert_bucket, file_key)
    print('finished.')

    # Send job request to tx-manager
    source_url = 'https://s3-us-west-2.amazonaws.com/{0}/{1}'.format(pre_convert_bucket, file_key)  # we use us-west-2 for our s3 buckets
    tx_manager_job_url = api_url+'/tx/job'
    identifier = "{0}/{1}/{2}".format(repo_owner, repo_name, commit_id[:10])  # The way to know which repo/commit goes to this job request
    payload = {
        "identifier": identifier,
        "user_token": gogs_user_token,
        "resource_type": resource_type,
        "input_format": input_format,
        "output_format": "html",
        "source": source_url,
        "callback": api_url+'/client/callback'
    }
    headers = {"content-type": "application/json"}

    print('Making request to tx-Manager URL {0} with payload:'.format(tx_manager_job_url))
    print(payload)
    print('...', end=' ')
    response = requests.post(tx_manager_job_url, json=payload, headers=headers)
    print('finished.')

    # for testing
    print('tx-manager response:')
    print(response)

    if not response:
        raise Exception('Bad request: unable to convert')

    if 'errorMessage' in response:
        raise Exception('Bad request: '.format(response['errorMessage']))

    json_data = json.loads(response.text)
    print("json:")
    print(json_data)

    if 'job' not in json_data:
        raise Exception('Bad request: Did not receive a response from the tX Manager')

    build_log_json = {
        'job_id': json_data['job']['job_id'],
        'repo_name': repo_name,
        'repo_owner': repo_owner,
        'commit_id': commit_id,
        'committed_by': pusher_username,
        'commit_url': commit_url,
        'compare_url': compare_url,
        'commit_message': commit_message,
        'created_at': datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        'eta': json_data['job']['eta'],
        'resource_type': resource_type,
        'input_format': input_format,
        'output_format': 'html',
        'success': None,
        'status': 'started',
        'message': 'Conversion in progress...'
    }

    if 'errorMessage' in json_data:
        build_log_json['status'] = 'failed'
        build_log_json['message'] = json_data['errorMessage']

    # Upload files to S3:

    # S3 location vars
    s3_commit_key = 'u/{0}'.format(identifier)
    s3_resource = boto3.resource('s3')
    bucket = s3_resource.Bucket(cdn_bucket)

    # Remove everything in the bucket with the s3_commit_key prefix so old files are removed, if any
    for obj in bucket.objects.filter(Prefix=s3_commit_key):
        s3_resource.Object(bucket.name, obj.key).delete()

    # Make a build_log.json file with this repo and commit data for later processing, upload to S3
    build_log_file = os.path.join(tempfile.gettempdir(), 'build_log_request.json')
    write_file(build_log_file, build_log_json)
    bucket.upload_file(build_log_file, s3_commit_key+'/build_log.json', ExtraArgs={'ContentType': 'application/json', 'CacheControl': 'max-age=0'})
    print('Uploaded the following content from {0} to {1}/build_log.json'.format(build_log_file, s3_commit_key))
    print(build_log_json)

    # Upload the manifest.json file to the cdn_bucket if it exists
    if os.path.isfile(manifest_filepath):
        bucket.upload_file(manifest_filepath, s3_commit_key+'/manifest.json', ExtraArgs={'ContentType': 'application/json', 'CacheControl': 'max-age=0'})
        print('Uploaded the manifest.json file to {0}/manifest.json'.format(s3_commit_key))

    # If there was an error, in order to trigger a 400 error in the API Gateway, we need to raise an
    # exception with the returned 'errorMessage' because the API Gateway needs to see 'Bad Request:' in the string
    if 'errorMessage' in json_data:
        raise Exception(json_data['errorMessage'])

    return build_log_json
