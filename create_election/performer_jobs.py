# -*- coding: utf-8 -*-

# This file is part of election-orchestra.
# Copyright (C) 2013  Eduardo Robles Elvira <edulix AT wadobo DOT com>

# election-orchestra is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License.

# election-orchestra  is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with election-orchestra.  If not, see <http://www.gnu.org/licenses/>.

import re
import os
import codecs
import subprocess
import json
import shutil
from datetime import datetime

from frestq import decorators
from frestq.utils import dumps, loads
from frestq.tasks import SimpleTask, ParallelTask, ExternalTask, TaskError
from frestq.protocol import certs_differ
from frestq.app import app, db

from models import Election, Authority, Session
from utils import *

def check_election_data(data, check_extra):
    '''
    check election input data. Used both in public_api.py:post_election and
    generate_private_info.
    '''
    requirements = [
        {'name': u'election_id', 'isinstance': basestring},
        {'name': u'title', 'isinstance': basestring},
        {'name': u'url', 'isinstance': basestring},
        {'name': u'description', 'isinstance': basestring},
        {'name': u'is_recurring', 'isinstance': bool},
        {'name': u'authorities', 'isinstance': list},
    ]

    if check_extra:
        requirements += [
            {'name': 'callback_url', 'isinstance': basestring},
            {'name': 'extra', 'isinstance': list},
            {'name': u'questions_data', 'isinstance': list},
        ]
        questions_data = data.get('questions_data', None)
    else:
        try:
            questions_data = json.loads(data.get('questions_data', None))
        except:
            raise TaskError(dict(reason='questions_data is not in json'))

    for req in requirements:
        if req['name'] not in data or not isinstance(data[req['name']],
            req['isinstance']):
            raise TaskError(dict(reason="invalid %s parameter" % req['name']))

    if 'voting_start_date' not in data or (data['voting_start_date'] is not None
            and not isinstance(data['voting_start_date'], datetime)):
        raise TaskError(dict(reason="invalid voting_start_date parameter"))

    if 'voting_end_date' not in data or (data['voting_end_date'] is not None
            and not isinstance(data['voting_end_date'], datetime)):
        raise TaskError(dict(reason="invalid voting_send_date parameter"))

    if not re.match("^[a-zA-Z0-9_-]+$", data['election_id']):
        raise TaskError(dict(reason="invalid characters in election_id"))

    if len(data['authorities']) == 0:
        raise TaskError(dict(reason='no authorities'))

    if not isinstance(questions_data, list) or len(questions_data) < 1 or\
            len(questions_data) > app.config.get('MAX_NUM_QUESTIONS_PER_ELECTION', 10):
        raise TaskError(dict(reason='Unsupported number of questions in the election'))


    if check_extra and\
            Election.query.filter_by(id=data['election_id']).count() > 0:
        raise TaskError(dict(reason='an election with id %s already '
            'exists' % data['election_id']))

    auth_reqs = [
        {'name': 'name', 'isinstance': basestring},
        {'name': 'orchestra_url', 'isinstance': basestring},
        {'name': 'ssl_cert', 'isinstance': basestring},
    ]

    for adata in data['authorities']:
        for req in auth_reqs:
            if req['name'] not in adata or not isinstance(adata[req['name']],
                req['isinstance']):
                raise TaskError(dict(reason="invalid %s parameter" % req['name']))

    def unique_by_keys(l, keys):
        for k in keys:
            if len(l) != len(set([i[k] for i in l])):
                return False
        return True

    if not unique_by_keys(data['authorities'], ['ssl_cert', 'orchestra_url']):
        raise TaskError(dict(reason="invalid authorities parameters"))

@decorators.task(action="generate_private_info", queue="orchestra_performer")
def generate_private_info(task):
    '''
    Generates the local private info for a new election
    '''
    input_data = task.get_data()['input_data']
    election_id = input_data['election_id']

    # 1. check this is a new election and check input data
    private_data_path = app.config.get('PRIVATE_DATA_PATH', '')
    election_privpath = os.path.join(private_data_path, election_id)

    # check generic input data, similar to the data for public_api
    check_election_data(input_data, False)

    # check the sessions data
    if not isinstance(input_data.get('sessions', None), list) or\
            not len(input_data['sessions']):
        raise TaskError(dict(reason="No sessions provided"))
    for session in input_data['sessions']:
        if not isinstance(session, dict) or 'id' not in session or\
                'stub' not in session or\
                not isinstance(session['stub'], basestring) or\
                not re.match("^[a-zA-Z0-9_-]+$", session['id']):
            raise TaskError(dict(reason="Invalid session data provided"))


    # check that we are indeed one of the listed authorities
    auth_name = None
    for auth_data in input_data['authorities']:
        if auth_data['orchestra_url'] == app.config.get('ROOT_URL', ''):
            auth_name = auth_data['name']
    if not auth_name:
        raise TaskError(dict(reason="trying to process what SEEMS to be an external election"))

    # localProtInfo.xml should not exist for any of the sessions, as our task is
    # precisely to create it. note that we only check that localProtInfo.xml
    # files don't exist, because if we are the director, then the stub and
    # parent directory will already exist
    for session in input_data['sessions']:
        session_privpath = os.path.join(election_privpath, session['id'])
        protinfo_path = os.path.join(session_privpath, 'localProtInfo.xml')
        if os.path.exists(protinfo_path):
            raise TaskError(dict(reason="session_id %s already created" % session['id']))

    # 2. create base local data from received input in case it's needed:
    # create election models, dirs and stubs if we are not the director
    if certs_differ(task.get_data()['sender_ssl_cert'], app.config.get('SSL_CERT_STRING', '')):
        if os.path.exists(election_privpath):
            raise TaskError(dict(reason="Already existing election id %s" % input_data['election_id']))
        election = Election(
            id = input_data['election_id'],
            title = input_data['title'],
            url = input_data['url'],
            description = input_data['description'],
            questions_data = input_data['questions_data'],
            voting_start_date = input_data['voting_start_date'],
            voting_end_date = input_data['voting_end_date'],
            is_recurring = input_data['is_recurring'],
            num_parties = input_data['num_parties'],
            threshold_parties = input_data['threshold_parties'],
        )
        db.session.add(election)

        for auth_data in input_data['authorities']:
            authority = Authority(
                name = auth_data['name'],
                ssl_cert = auth_data['ssl_cert'],
                orchestra_url = auth_data['orchestra_url'],
                election_id = input_data['election_id']
            )
            db.session.add(authority)

        # create dirs and stubs, and session model
        i = 0
        for session in input_data['sessions']:
            session_model = Session(
                id=session['id'],
                election_id=election_id,
                status='default',
                public_key='',
                question_number=i
            )
            db.session.add(session_model)

            session_privpath = os.path.join(election_privpath, session['id'])
            mkdir_recursive(session_privpath)
            stub_path = os.path.join(session_privpath, 'stub.xml')
            stub_file = codecs.open(stub_path, 'w', encoding='utf-8')
            stub_content = stub_file.write(session['stub'])
            stub_file.close()
            i += 1
        db.session.commit()
    else:
        # if we are the director, models, dirs and stubs have been created
        # already, so we just get the election from the database
        election = db.session.query(Election)\
            .filter(Election.id == election_id).first()

    # only create external task if we have configured autoaccept to false in
    # settings:
    autoaccept = app.config.get('AUTOACCEPT_REQUESTS', '')
    if not autoaccept:
        def str_date(date):
            if date:
                return date.isoformat()
            else:
                return ""

        label = "approve_election"
        info_text = """* URL: %(url)s
* Title: %(title)s
* Description: %(description)s
* Voting period: %(start_date)s - %(end_date)s
* Question data: %(questions_data)s
* Authorities: %(authorities)s""" % dict(
            url = input_data['url'],
            title = election.title,
            description = election.description,
            start_date = str_date(election.voting_start_date),
            end_date = str_date(election.voting_end_date),
            questions_data = dumps(loads(input_data['questions_data']), indent=4),
            authorities = dumps(input_data['authorities'], indent=4)
        )
        approve_task = ExternalTask(label=label,
            data=info_text)
        task.add(approve_task)

    verificatum_task = SimpleTask(
        receiver_url=app.config.get('ROOT_URL', ''),
        action="generate_private_info_verificatum",
        queue="orchestra_performer",
        data=dict())
    task.add(verificatum_task)

@decorators.task(action="generate_private_info_verificatum", queue="orchestra_performer")
@decorators.local_task
def generate_private_info_verificatum(task):
    '''
    After the task has been approved, execute verificatum to generate the
    private info
    '''
    # first of all, check that parent task is approved, but we only check that
    # when autoaccept is configured to False. if that's not the case,
    # then cancel everything
    autoaccept = app.config.get('AUTOACCEPT_REQUESTS', '')
    if not autoaccept and\
            task.get_prev().get_data()['output_data'] != dict(status="accepted"):
        task.set_output_data("task not accepted")
        raise TaskError(dict(reason="task not accepted"))

    input_data = task.get_parent().get_data()['input_data']
    election_id = input_data['election_id']
    sessions = input_data['sessions']
    election = db.session.query(Election)\
        .filter(Election.id == election_id).first()

    auth_name = None
    for auth_data in input_data['authorities']:
        if auth_data['orchestra_url'] == app.config.get('ROOT_URL', ''):
            auth_name = auth_data['name']

    private_data_path = app.config.get('PRIVATE_DATA_PATH', '')
    election_privpath = os.path.join(private_data_path, election_id)

    # this are an "indicative" url, because port can vary later on
    server_url = get_server_url()
    hint_server_url = get_hint_server_url()

    # generate localProtInfo.xml
    protinfos = []
    for session in sessions:
        session_privpath = os.path.join(election_privpath, session['id'])
        protinfo_path = os.path.join(session_privpath, 'localProtInfo.xml')
        stub_path = os.path.join(session_privpath, 'stub.xml')

        l = ["vmni", "-party", "-arrays", "file", "-name", auth_name, "-http",
            server_url, "-hint", hint_server_url]
        subprocess.check_call(l, cwd=session_privpath)

        # 5. read local protinfo file to be sent back to the orchestra director
        protinfo_file = codecs.open(protinfo_path, 'r', encoding='utf-8')
        protinfos.append(protinfo_file.read())
        protinfo_file.close()

    # set the output data of parent task, and update sender
    task.get_parent().set_output_data(protinfos, send_update_to_sender=True)

@decorators.task(action="generate_public_key", queue="verificatum_queue")
def generate_public_key(task):
    '''
    Generates the local private info for a new election
    '''
    input_data = task.get_data()['input_data']
    session_id = input_data['session_id']
    election_id = input_data['election_id']

    privdata_path = app.config.get('PRIVATE_DATA_PATH', '')
    session_privpath = os.path.join(privdata_path, election_id, session_id)

    # some sanity checks, as this is not a local task
    if not os.path.exists(session_privpath):
        raise TaskError(dict(reason="invalid session_id / election_id"))
    if os.path.exists(os.path.join(session_privpath, 'publicKey_raw')) or\
            os.path.exists(os.path.join(session_privpath, 'publicKey_json')):
        raise TaskError(dict(reason="pubkey already created"))

    # if it's not local, we have to create the merged protInfo.xml
    protinfo_path = os.path.join(session_privpath, 'protInfo.xml')
    if not os.path.exists(protinfo_path):
        protinfo_file = codecs.open(protinfo_path, 'w', encoding='utf-8')
        protinfo_file.write(input_data['protInfo_content'])
        protinfo_file.close()

    # generate raw public key
    subprocess.check_call(["vmn", "-keygen", "publicKey_raw"], cwd=session_privpath)

    # transform it into json format
    subprocess.check_call(["vmnc", "-pkey", "-outi", "json", "publicKey_raw",
                           "publicKey_json"], cwd=session_privpath)

    # publish protInfo.xml and publicKey_json
    pubdata_path = app.config.get('PUBLIC_DATA_PATH', '')
    session_pubpath = os.path.join(pubdata_path, election_id, session_id)
    if not os.path.exists(session_pubpath):
        mkdir_recursive(session_pubpath)

    pubkey_privpath = os.path.join(session_privpath, 'publicKey_json')
    pubkey_pubpath = os.path.join(session_pubpath, 'publicKey_json')
    shutil.copyfile(pubkey_privpath, pubkey_pubpath)

    protinfo_privpath = os.path.join(session_privpath, 'protInfo.xml')
    protinfo_pubpath = os.path.join(session_pubpath, 'protInfo.xml')
    shutil.copyfile(protinfo_privpath, protinfo_pubpath)