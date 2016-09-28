# -*- coding: utf-8 -*-

# Method for receiving a callback from tX Manager, can do something here such as email the user

# Python libraries
import os
import tempfile
import boto3
import json
import requests

# Functions from Python libraries
from mimetypes import MimeTypes

# Functions from unfoldingWord libraries
from general_tools.file_utils import unzip, write_file
from general_tools.url_utils import download_file, get_url
from aws_tools.s3_handler import S3Handler

def handle(event, context):
    # Getting data from payload which is the JSON that was sent from tx-manager
    if 'data' not in event:
        raise Exception('"data" not in payload')
    job = event['data']

    env_vars = {}
    if 'vars' in event and isinstance(event['vars'], dict):
        env_vars = event['vars']

    # Getting the bucket to where we will unzip the converted files for door43.org. It is different from
    # production and testing, thus it is an environment variable the API Gateway gives us
    if 'cdn_bucket' not in env_vars:
        raise Exception('"cdn_bucket" was not in payload')
    cdn_handler = S3Handler(env_vars['cdn_bucket'])

    if 'identifier' not in job or not job['identifier']:
        raise Exception('"identifier" not in payload')

    owner_name, repo_name, commit_id = job['identifier'].split('/')

    s3_commit_key = 'u/{0}/{1}/{2}'.format(owner_name, repo_name, commit_id)  # The identifier is how to know which username/repo/commit this callback goes to

    # Download the ZIP file of the converted files
    converted_zip_url = job['output']
    converted_zip_file = os.path.join(tempfile.gettempdir(), converted_zip_url.rpartition('/')[2])
    try:
        print('Downloading converted zip file from {0}...'.format(converted_zip_url))
        if not os.path.isfile(converted_zip_file):
            download_file(converted_zip_url, converted_zip_file)
    finally:
        print('finished.')

    # Unzip the archive
    unzip_dir = tempfile.mkdtemp(prefix='unzip_')
    try:
        print('Unzipping {0}...'.format(converted_zip_file))
        unzip(converted_zip_file, unzip_dir)
    finally:
        print('finished.')

    # Upload all files to the cdn_bucket with the key of <user>/<repo_name>/<commit> of the repo
    for root, dirs, files in os.walk(unzip_dir):
        for f in sorted(files):
            path = os.path.join(root, f)
            key = s3_commit_key + path.replace(unzip_dir, '')
            print('Uploading {0} to {1}'.format(f, key))
            cdn_handler.upload_file(path, key)

    # Now download the existing build_log.json file, update it and upload it back to S3
    build_log_json = cdn_handler.get_json(s3_commit_key+'/build_log.json')

    build_log_json['started_at'] = job['started_at']
    build_log_json['ended_at'] = job['ended_at']
    build_log_json['success'] = job['success']
    build_log_json['status'] = job['status']

    if 'log' in job and job['log']:
        build_log_json['log'] = job['log']
    else:
        build_log_json['log'] = []

    if 'warnings' in job and job['warnings']:
        build_log_json['warnings'] = job['warnings']
    else:
        build_log_json['warnings'] = []

    if 'errors' in job and job['errors']:
        build_log_json['errors'] = job['errors']
    else:
        build_log_json['errors'] = []

    build_log_file = os.path.join(tempfile.gettempdir(), 'build_log_finished.json')
    write_file(build_log_file, build_log_json)
    cdn_handler.upload_file(build_log_file, s3_commit_key+'/build_log.json', 0)

    # Download the project.json file for this repo (create it if doesn't exist) and update it
    project_json_key = 'u/{0}/{1}/project.json'.format(owner_name, repo_name)
    project_json = cdn_handler.get_json(project_json_key)

    project_json['user'] = owner_name
    project_json['repo'] = repo_name
    project_json['repo_url'] = 'https://git.door43.org/{0}/{1}'.format(owner_name, repo_name)
    
    commit = {
        'id': commit_id,
        'created_at': job['created_at'],
        'status': job['status'],
        'success': job['success'],
        'started_at': None,
        'ended_at': None
    }
    if 'started_at' in job:
        commit['started_at'] = job['started_at']
    if 'ended_at' in job:
        commit['ended_at'] = job['ended_at']

    if 'commits' not in project_json:
        project_json['commits'] = []
    project_json['commits'].append(commit)

    project_file = os.path.join(tempfile.gettempdir(), 'project.json')
    write_file(project_file, project_json)
    cdn_handler.upload_file(project_file, project_json_key, 0)

    print('Finished deploying to cdn_bucket. Done.')


