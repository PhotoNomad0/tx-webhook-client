# -*- coding: utf-8 -*-

#
# Webhook client for receiving a payload from Gogs to process the job
#

from __future__ import print_function

import os
import sys
import json
import tempfile
import requests

from general_tools.file_utils import unzip, get_files, get_subdirs, write_file, add_contents_to_zip, add_file_to_zip
from general_tools.url_utils import download_file
from door43_tools import repo_compilers
from door43_tools.manifest_handler import Manifest, MetaData
from aws_tools.s3_handler import S3Handler
from door43_tools.bible_books import BOOK_NAMES


def str_to_class(str):
    """
    Gets a class from a string.
    :param str|unicode str: The string of the class name
    """
    return reduce(getattr, str.split("."), sys.modules[__name__])


def download_repo(commit_url, repo_dir):
    repo_zip_url = commit_url.replace('commit', 'archive') + '.zip'
    repo_zip_file = os.path.join(tempfile.gettempdir(), repo_zip_url.rpartition('/')[2])
    try:
        print('Downloading {0}...'.format(repo_zip_url))
        if not os.path.isfile(repo_zip_file):
            download_file(repo_zip_url, repo_zip_file)
    finally:
        print('finished.')

    try:
        print('Unzipping {0}...'.format(repo_zip_file))
        unzip(repo_zip_file, repo_dir)
    finally:
        print('finished.')


def handle(event, context):
    # Get vars and data
    env_vars = retrieve(event, 'vars', 'payload')
    api_url = retrieve(env_vars, 'api_url', 'Environment Vars')
    pre_convert_bucket = retrieve(env_vars, 'pre_convert_bucket', 'Environment Vars')
    cdn_bucket = retrieve(env_vars, 'cdn_bucket', 'Environment Vars')
    gogs_url = retrieve(env_vars, 'gogs_url', 'Environment Vars')
    gogs_user_token = retrieve(env_vars, 'gogs_user_token', 'Environment Vars')
    repo_commit = retrieve(event, 'data', 'payload')

    commit_id = repo_commit['after']
    commit = None
    for commit in repo_commit['commits']:
        if commit['id'] == commit_id:
            break

    commit_url = commit['url']
    commit_message = commit['message']

    if gogs_url not in commit_url:
        raise Exception('Repos can only belong to {0} to use this webhook client.'.format(gogs_url))

    repo_name = repo_commit['repository']['name']
    repo_owner = repo_commit['repository']['owner']['username']
    compare_url = repo_commit['compare_url']

    if 'pusher' in repo_commit:
        pusher = repo_commit['pusher']
    else:
        pusher = {'username': commit['author']['username']}
    pusher_username = pusher['username']

    # 1) Download and unzip the repo files
    repo_dir = tempfile.mkdtemp(prefix='repo_')
    download_repo(commit_url, repo_dir)
    repo_dir = os.path.join(repo_dir, repo_name)

    # 2) Get the manifest file or make one if it doesn't exist based on meta.json, repo_name and file extensions
    manifest_path = os.path.join(repo_dir, 'manifest.json')
    if not os.path.isfile(manifest_path):
        manifest_path = os.path.join(repo_dir, 'project.json')
        if not os.path.isfile(manifest_path):
            manifest_path = None
    meta_path = os.path.join(repo_dir, 'meta.json')
    meta = None
    if os.path.isfile(meta_path):
        meta = MetaData(meta_path)
    files = get_files(repo_dir, True)
    manifest = Manifest(file_name=manifest_path, repo_name=repo_name, files=files, meta=meta)

    manifest_path = os.path.join(repo_dir, 'manifest.json')
    write_file(manifest_path, manifest.__dict__)  # Write it back out so it's using the latest manifest format

    # determining the repo compiler:
    dirs = sorted(get_subdirs(repo_dir, True))
    generator = ''
    if manifest.generator and manifest.generator['name'] and manifest.generator['name'].startswith('ts-'):
        generator = 'ts'
    elif manifest.format == 'usfm' and manifest.project and manifest.project['id'] in BOOK_NAMES:
        for directory in dirs:
                try:
                    int(directory)
                    manifest.generator = 'ts'
                    break
                except Exception:
                    continue
    if not manifest.generator:
        if 'content' in dirs:
            repo_dir = os.path.join(repo_dir, 'content')
        elif 'usfm' in dirs:
            repo_dir = os.path.join(repo_dir, 'usfm')

    input_format = manifest.format

    print(dirs)
    print(generator)
    print(input_format)
    print(manifest.__dict__)
    try:
        compiler_class = str_to_class('repo_compilers.{0}{1}RepoCompiler'.format(generator.capitalize(),
                                                                                    input_format.capitalize()))
    except AttributeError as e:
        print('Got AE: {0}'.format(e.message))
        compiler_class = repo_compilers.RepoCompiler

    print(compiler_class)

    # merge the source files with the template
    output_dir = tempfile.mkdtemp(prefix='output_')
    compiler = compiler_class(manifest, repo_dir, output_dir)
    compiler.run()

    # 3) Zip up the massaged files
    zip_filename = context.aws_request_id + '.zip'  # context.aws_request_id is a unique ID for this lambda call, so using it to not conflict with other requests
    zip_filepath = os.path.join(tempfile.gettempdir(), zip_filename)
    print('Zipping files from {0} to {1}...'.format(output_dir, zip_filepath))
    add_contents_to_zip(zip_filepath, output_dir)
    if os.path.isfile(manifest_path) and not os.path.isfile(os.path.join(output_dir, 'manifest.json')):
        add_file_to_zip(zip_filepath, manifest_path, 'manifest.json')
    print('finished.')

    # 4) Upload zipped file to the S3 bucket (you may want to do some try/catch and give an error if fails back to Gogs)
    print('Uploading {0} to {1}...'.format(zip_filepath, pre_convert_bucket))
    s3_handler = S3Handler(pre_convert_bucket)
    file_key = "preconvert/" + zip_filename
    s3_handler.upload_file(zip_filepath, file_key)
    print('finished.')

    # Send job request to tx-manager
    source_url = 'https://s3-us-west-2.amazonaws.com/{0}/{1}'.format(pre_convert_bucket,
                                                                     file_key)  # we use us-west-2 for our s3 buckets
    tx_manager_job_url = api_url + '/tx/job'
    identifier = "{0}/{1}/{2}".format(repo_owner, repo_name,
                                      commit_id[:10])  # The way to know which repo/commit goes to this job request
    payload = {
        "identifier": identifier,
        "user_token": gogs_user_token,
        "resource_type": manifest.resource['id'],
        "input_format": input_format,
        "output_format": "html",
        "source": source_url,
        "callback": api_url + '/client/callback'
    }
    headers = {"content-type": "application/json"}

    print('Making request to tx-Manager URL {0} with payload:'.format(tx_manager_job_url))
    print(payload)
    print('...')
    response = requests.post(tx_manager_job_url, json=payload, headers=headers)
    print('finished.')

    # for testing
    print('tx-manager response:')
    print(response)

    if not response:
        raise Exception('Bad request: unable to convert')

    if 'errorMessage' in response:
        raise Exception('Bad request: {0}'.format(response['errorMessage']))

    json_data = json.loads(response.text)

    if 'errorMessage' in json_data:
        raise Exception('Bad request: {0}'.format(json_data['errorMessage']))

    if 'job' not in json_data:
        raise Exception('Bad request: tX Manager did not return any info about the job request.')
    build_log_json = json_data['job']

    build_log_json['repo_name'] = repo_name
    build_log_json['repo_owner'] = repo_owner
    build_log_json['commit_id'] = commit_id
    build_log_json['committed_by'] = pusher_username
    build_log_json['commit_url'] = commit_url
    build_log_json['compare_url'] = compare_url
    build_log_json['commit_message'] = commit_message

    if 'errorMessage' in json_data:
        build_log_json['status'] = 'failed'
        build_log_json['message'] = json_data['errorMessage']

    # Upload files to S3:

    # S3 location vars
    cdn_handler = S3Handler(cdn_bucket)
    s3_commit_key = 'u/{0}'.format(identifier)

    # Remove everything in the bucket with the s3_commit_key prefix so old files are removed, if any
    for obj in cdn_handler.get_objects(prefix=s3_commit_key):
        cdn_handler.delete_file(obj.key)

    # Make a build_log.json file with this repo and commit data for later processing, upload to S3
    build_log_file = os.path.join(tempfile.gettempdir(), 'build_log_request.json')
    write_file(build_log_file, build_log_json)
    cdn_handler.upload_file(build_log_file, s3_commit_key + '/build_log.json', 0)

    # Upload the manifest.json file to the cdn_bucket if it exists
    if os.path.isfile(manifest_path):
        cdn_handler.upload_file(manifest_path, s3_commit_key + '/manifest.json', 0)

    # If there was an error, in order to trigger a 400 error in the API Gateway, we need to raise an
    # exception with the returned 'errorMessage' because the API Gateway needs to see 'Bad Request:' in the string
    if 'errorMessage' in json_data:
        raise Exception('Bad Request: {0}'.format(json_data['errorMessage']))

    return build_log_json


def retrieve(dictionary, key, dict_name=None):
    """
    Retrieves a value from a dictionary, raising an error message if the
    specified key is not valid
    :param dict dictionary:
    :param any key:
    :param str|unicode dict_name: name of dictionary, for error message
    :return: value corresponding to key
    """
    if key in dictionary:
        return dictionary[key]
    dict_name = "dictionary" if dict_name is None else dict_name
    raise Exception("Bad Request: {k} not found in {d}".format(k=repr(key), d=dict_name))
