"""Права, очистка и недоступность Monium не меняют результат приложения."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from api.auth import SESSION_COOKIE, timestamp
from api.handler import handler
from api.diagnostics import export

spec = importlib.util.spec_from_file_location('accounts_helpers', Path(__file__).with_name('test_accounts_programs.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.AccountsProgramsTests(methodName='runTest')
        self.h.setUp()
        self.payload = {'expected_owner_id': 'alice', 'events': [{'event_id': str(uuid4()),
            'at': timestamp(self.h.clock), 'name': 'local_save_failed', 'code': 'storage',
            'duration_ms': None, 'count': 1}]}

    def tearDown(self):
        self.h.tearDown()

    def send(self, payload=None, token=None, origin='https://gym.example'):
        token = token or self.h.alice
        session = self.h.auth.session(token)
        with patch('api.handler.application', return_value=self.h.auth):
            return handler({'httpMethod': 'POST', 'path': '/api/v1/telemetry', 'headers': {
                'Cookie': SESSION_COOKIE + '=' + token, 'Origin': origin,
                'X-CSRF-Token': session['csrf_token'], 'Content-Type': 'application/json'},
                'body': json.dumps(payload or self.payload)})

    def test_strict_sanitizing_owner_csrf_and_durable_limit(self):
        with patch('api.diagnostics.export', return_value=True) as sink:
            self.assertEqual(self.send()['statusCode'], 200)
            self.assertEqual(self.send()['statusCode'], 200)
            self.assertEqual(self.send()['statusCode'], 429)
            records = [x for call in sink.call_args_list for x in call.args[0] if x.get('event') == 'local_save_failed']
            self.assertEqual(records[0], {'event': 'local_save_failed', 'code': 'storage', 'duration_ms': None, 'count': 1})
            self.assertNotIn('alice', json.dumps(records))
        self.assertEqual(self.send(token=self.h.bob)['statusCode'], 403)
        self.assertEqual(self.send(origin='https://foreign.example')['statusCode'], 403)
        bad = json.loads(json.dumps(self.payload)); bad['events'][0]['code'] = 'secret_token'
        self.assertEqual(self.send(bad)['statusCode'], 400)

    def test_export_failure_is_bounded_and_does_not_fail_http(self):
        with patch.dict(os.environ, {'GYM_MONIUM_API_KEY': 'must-not-escape', 'GYM_MONIUM_PROJECT': 'folder__test'}), \
             patch('api.diagnostics.build_opener') as opener:
            opener.return_value.open.side_effect = TimeoutError('must-not-escape')
            response = self.send()
            self.assertEqual(response['statusCode'], 200)
            self.assertEqual(json.loads(response['body']), {'accepted': 0, 'dropped': 1})
            self.assertNotIn('must-not-escape', response['body'])
            self.assertLessEqual(opener.return_value.open.call_args.kwargs['timeout'], 1)
            self.assertFalse(export([{'event': 'probe'}]))
            self.assertEqual(handler({'httpMethod': 'GET','path': '/api/v1/health'})['statusCode'], 200)

    def test_request_log_uses_template_and_never_path_or_body(self):
        with patch('api.diagnostics.export', return_value=True) as sink:
            response = handler({'httpMethod':'GET','path':'/api/v1/workouts/private_owner_id',
                'headers':{'Authorization':'Bearer secret-token'},'body':''})
            record = sink.call_args.args[0][0]
            self.assertEqual(record['route'], '/api/v1/workouts/{workout_id}')
            self.assertNotIn('private_owner_id', json.dumps(record))
            self.assertNotIn('secret-token', json.dumps(record))
            self.assertEqual(record['status'], response['statusCode'])

    def test_client_build_version_is_format_checked_and_logged_without_identity(self):
        with patch('api.diagnostics.export', return_value=True) as sink:
            self.assertEqual(self.send(self.payload | {'app_version': 'a' * 40})['statusCode'], 200)
            records = [record for call in sink.call_args_list for record in call.args[0]
                if record.get('event') == 'local_save_failed']
            self.assertEqual(records[0]['app_version'], 'a' * 40)
            self.assertNotIn('expected_owner_id', records[0])
        self.assertEqual(self.send(self.payload | {'app_version': 'token-or-personal-text'})['statusCode'], 400)

    def test_readiness_checks_database_and_liveness_survives_its_failure(self):
        event = {'httpMethod': 'GET', 'path': '/api/v1/health/ready'}
        with patch('api.handler.application', return_value=self.h.auth):
            self.assertEqual(handler(event)['statusCode'], 200)
            with patch.object(self.h.auth.store, 'run', side_effect=TimeoutError('sensitive internal detail')):
                result = handler(event)
                self.assertEqual(result['statusCode'], 503)
                self.assertNotIn('sensitive', result['body'])
                self.assertEqual(handler(event | {'path': '/api/v1/health'})['statusCode'], 200)
