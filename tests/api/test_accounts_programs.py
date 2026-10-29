"""Инварианты аккаунтов/публикаций; те же сценарии выполняются в CI на YDB."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import sqlalchemy as sa

from api.auth import Auth, AuthSettings, YandexProvider, cookies, digest, SESSION_COOKIE, LOGIN_COOKIE, IDLE, ABSOLUTE
from api.errors import ServiceError
from api.handler import handler
from api.programs import Programs
from api.store import Store
from contracts.validation import validate

EXAMPLES = json.loads((Path(__file__).resolve().parents[2] / 'contracts/examples.json').read_text(encoding='utf-8'))['examples']
CONTENT = next(e['output']['version']['content'] for e in EXAMPLES if e['operation'] == 'get_program')


class ApplicationCases:
    def prepare(self):
        self.clock = 1793275200
        self.store = Store(self.engine, self.prefix)
        self.provider = Mock()
        self.auth = Auth(self.store, AuthSettings('https://gym.example', 'a' * 32, 'synthetic-client-secret'),
                         self.provider, now=lambda: self.clock)
        self.programs = Programs(self.auth)
        self.alice = self.login('101', 'alice')
        self.bob = self.login('102', 'bob')

    def allow(self, subject, owner):
        self.store.run(lambda db: db.put('admissions', 'yandex', digest('yandex:' + subject),
            {'user_id': owner, 'state': 'active'}))

    def begin(self, header='', **query):
        location, headers = self.auth.begin(query, header)
        state = parse_qs(urlsplit(location).query)['state'][0]
        binder = headers[0].split(';')[0]
        return state, binder

    def login(self, subject, owner):
        self.allow(subject, owner)
        self.provider.identity.return_value = subject
        state, binder = self.begin()
        _, headers = self.auth.callback({'state': state, 'code': 'synthetic-code'}, binder, 'trace')
        return headers[-1].split(';')[0].split('=', 1)[1]

    def error(self, code, call):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def execute(self, operation, token=None, **value):
        return self.programs.execute(operation, token or self.alice, value)

    def propose(self, token=None, owner='alice', base=None, weight=60000):
        content = copy.deepcopy(CONTENT)
        content['templates'][0]['exercises'][0]['targets'][0]['weight_g'] = weight
        return self.execute('propose_change', token, request_id=str(uuid4()), expected_owner_id=owner,
            proposal={'base_program_version_id': base, 'content': content, 'rationale': 'Согласованные цели'})

    def approve_input(self, change, owner='alice'):
        return {'request_id': str(uuid4()), 'expected_owner_id': owner,
            'change_id': change['change_id'], 'expected_revision': change['change_revision'],
            'base_program_version_id': change['proposal']['base_program_version_id'],
            'content_digest': change['content_digest']}

    def publish(self, change, token=None, owner='alice'):
        self.execute('validate_change', token, change_id=change['change_id'], expected_revision=change['change_revision'])
        return self.programs.execute('approve_and_publish', token or self.alice, self.approve_input(change, owner))

    def test_two_users_cannot_read_or_mutate_each_others_objects(self):
        change = self.propose()
        self.error('not_found', lambda: self.execute('get_change', self.bob, change_id=change['change_id']))
        self.error('account_mismatch', lambda: self.execute('propose_change', self.bob,
            request_id=str(uuid4()), expected_owner_id='alice', proposal=change['proposal']))
        self.error('not_found', lambda: self.programs.execute('approve_and_publish', self.bob,
            self.approve_input(change, 'bob')))
        self.publish(change)
        self.assertIsNone(self.execute('get_program', self.bob)['version'])
        self.assertEqual(self.execute('get_program')['owner_id'], 'alice')

    def test_published_versions_immutable_and_stale_base_rejected(self):
        first, stale = self.propose(), self.propose(weight=70000)
        initial = self.publish(first)['program_version']
        self.error('program_conflict', lambda: self.publish(stale))
        next_change = self.propose(base=initial['program_version_id'], weight=65000)
        current = self.publish(next_change)['program_version']
        self.assertEqual(current['version'], 2)
        old = self.store.run(lambda db: db.get('program_records', 'alice', 'version:' + initial['program_version_id']))
        self.assertEqual(old, initial)
        validate('ProgramVersion', current)

    def test_publication_repeat_and_request_id_reuse(self):
        change = self.propose()
        self.execute('validate_change', change_id=change['change_id'], expected_revision=1)
        value = self.approve_input(change)
        first = self.programs.execute('approve_and_publish', self.alice, value)
        self.assertEqual(self.programs.execute('approve_and_publish', self.alice, value), first)
        self.execute('validate_change', change_id=change['change_id'], expected_revision=1)
        modified = value | {'content_digest': '0' * 64}
        self.error('request_id_reused', lambda: self.programs.execute('approve_and_publish', self.alice, modified))
        later = value | {'request_id': str(uuid4())}
        self.assertEqual(self.programs.execute('approve_and_publish', self.alice, later), first)
        self.assertEqual(self.execute('get_program')['version']['version'], 1)

    def test_approval_bound_to_revision_digest_and_validated_content(self):
        change = self.propose()
        value = self.approve_input(change)
        self.error('validation_failed', lambda: self.programs.execute('approve_and_publish', self.alice, value))
        self.execute('validate_change', change_id=change['change_id'], expected_revision=1)
        self.error('approval_invalid', lambda: self.programs.execute('approve_and_publish', self.alice,
            value | {'content_digest': '0' * 64}))
        changed = self.execute('replace_change', request_id=str(uuid4()), expected_owner_id='alice',
            change_id=change['change_id'], expected_revision=1,
            proposal=change['proposal'] | {'rationale': 'Новая редакция'})
        self.assertEqual(changed['change_revision'], 2)
        self.error('revision_conflict', lambda: self.programs.execute('approve_and_publish', self.alice, value))
        self.error('approval_required', lambda: self.execute('publish_approved', **(self.approve_input(changed) | {'approval_id': 'fake'})))
        self.assertIsNone(self.execute('get_program')['version'])

    def test_recent_authentication_needed_for_new_approval(self):
        change = self.propose()
        self.clock += 601
        self.error('session_required', lambda: self.publish(change))
        self.alice = self.login('101', 'alice')
        self.assertEqual(self.publish(change)['program_version']['version'], 1)

    def test_disabled_admission_blocks_existing_sessions_and_writes(self):
        self.store.run(lambda db: db.put('admissions', 'yandex', digest('yandex:101'), {'user_id': 'alice', 'state': 'disabled'}))
        self.error('account_disabled', lambda: self.auth.session(self.alice))
        self.error('account_disabled', lambda: self.propose())

    def test_idle_and_absolute_expiry_and_refresh(self):
        self.clock += IDLE - 1
        self.auth.session(self.alice)
        self.clock += IDLE
        self.error('session_required', lambda: self.auth.session(self.alice))
        self.alice = self.login('101', 'alice')
        for _ in range(4):
            self.clock += 6 * 86400
            self.auth.session(self.alice)
        self.clock += 6 * 86400
        self.error('session_required', lambda: self.auth.session(self.alice))

    def test_callback_consumed_before_provider_call_and_not_retried(self):
        state, binder = self.begin()
        self.provider.identity.side_effect = ServiceError('unavailable')
        self.error('unavailable', lambda: self.auth.callback({'state': state, 'code': 'secret-code'}, binder, 't'))
        saved = self.store.run(lambda db: db.get('login_transactions', 'login', digest(state)))
        self.assertNotIn('verifier', saved)
        self.provider.identity.side_effect = None
        self.error('invalid_request', lambda: self.auth.callback({'state': state, 'code': 'secret-code'}, binder, 't'))

    def test_uninvited_is_only_verified_request_and_never_admission(self):
        self.provider.identity.return_value = '103'
        state, binder = self.begin()
        self.error('not_invited', lambda: self.auth.callback({'state': state, 'code': 'code'}, binder, 'safe-ref'))
        admission, pending = self.store.run(lambda db: (db.get('admissions', 'yandex', digest('yandex:103')),
            db.get('login_requests', 'yandex', digest('yandex:103'))))
        self.assertIsNone(admission)
        self.assertEqual(pending['reference'], 'safe-ref')
        from infra.accounts_admin import admit
        self.assertIsNotNone(admit(self.store, 'safe-ref')['user_id'])
        admitted = self.store.run(lambda db: db.get('admissions', 'yandex', digest('yandex:103')))
        admit(self.store, 'safe-ref', state='disabled')
        self.assertEqual(admit(self.store, 'safe-ref')['user_id'], admitted['user_id'])

    def test_binder_state_expiry_reauth_identity_and_rotation(self):
        state, binder = self.begin()
        self.error('invalid_request', lambda: self.auth.callback({'state': state, 'code': 'code'}, LOGIN_COOKIE + '=' + 'z' * 43, 't'))
        self.clock += 300
        self.error('invalid_request', lambda: self.auth.callback({'state': state, 'code': 'code'}, binder, 't'))
        state, binder = self.begin(SESSION_COOKIE + '=' + self.alice, purpose='reauth')
        self.provider.identity.return_value = '102'
        self.error('account_mismatch', lambda: self.auth.callback({'state': state, 'code': 'code'}, binder, 't'))
        state, binder = self.begin(SESSION_COOKIE + '=' + self.alice)
        self.provider.identity.return_value = '101'
        self.auth.callback({'state': state, 'code': 'code'}, binder, 't')
        self.error('session_required', lambda: self.auth.session(self.alice))

    def test_http_csrf_query_path_and_owner_checks(self):
        def request(method, path, body=None, headers=None, query=None):
            return handler({'httpMethod': method, 'path': path, 'body': json.dumps(body) if body else '',
                'headers': {'Cookie': SESSION_COOKIE + '=' + self.alice, 'Content-Type': 'application/json', **(headers or {})},
                'multiValueQueryStringParameters': query or {}})
        with patch('api.handler.application', return_value=self.auth):
            self.assertEqual(request('GET', '/auth/session')['statusCode'], 200)
            value = next(e['input'] for e in EXAMPLES if e['operation'] == 'propose_change')
            value = copy.deepcopy(value) | {'request_id': str(uuid4()), 'expected_owner_id': 'alice'}
            self.assertEqual(request('POST', '/api/v1/program/changes', value)['statusCode'], 403)
            csrf = self.auth.session(self.alice)['csrf_token']
            headers = {'Origin': 'https://gym.example', 'X-CSRF-Token': csrf}
            self.assertEqual(request('POST', '/api/v1/program/changes', value,
                headers | {'Origin': 'https://attacker.example'})['statusCode'], 403)
            result = request('POST', '/api/v1/program/changes', value, headers)
            self.assertEqual(result['statusCode'], 200)
            change = json.loads(result['body'])
            validate('ProgramChange', change)
            self.assertEqual(request('GET', f"/api/v1/program/changes/{change['change_id']}",
                headers={'Cookie': SESSION_COOKIE + '=' + self.bob})['statusCode'], 404)
            self.assertEqual(request('GET', '/auth/login', query={'return_to': ['//evil.example']})['statusCode'], 400)
            self.assertEqual(request('GET', '/auth/login', query={'purpose': ['login', 'switch']})['statusCode'], 400)
            self.assertEqual(request('POST', f"/api/v1/program/changes/{change['change_id']}/validate",
                {'change_id': str(uuid4()), 'expected_revision': 1}, headers)['statusCode'], 400)
            self.assertEqual(request('POST', '/auth/logout', {}, headers)['statusCode'], 204)
            self.assertEqual(request('GET', '/auth/session')['statusCode'], 401)
            self.assertEqual(request('POST', '/auth/logout', {}, headers)['statusCode'], 204)

    def test_csrf_cookie_flags_and_duplicate_cookie(self):
        self.error('csrf_failed', lambda: self.auth.logout(None, {'origin': 'https://attacker.example'}))
        csrf = self.auth.session(self.alice)['csrf_token']
        self.error('csrf_failed', lambda: self.auth.check_write(self.alice,
            {'origin': 'https://gym.example', 'x-csrf-token': self.auth.session(self.bob)['csrf_token']}))
        self.auth.check_write(self.alice, {'origin': 'https://gym.example', 'x-csrf-token': csrf})
        _, headers = self.auth.begin({}, '')
        self.assertIn('Secure; HttpOnly; SameSite=Lax', headers[0])
        self.assertNotIn('Domain=', headers[0])
        self.error('session_required', lambda: cookies(f'{SESSION_COOKIE}={self.alice};{SESSION_COOKIE}={self.bob}'))

    def test_http_callback_ignores_provider_metadata_but_checks_security_fields(self):
        self.provider.identity.return_value = '101'
        def callback(query, binder):
            return handler({'httpMethod': 'GET', 'path': '/auth/callback/yandex',
                'headers': {'Cookie': binder}, 'multiValueQueryStringParameters': query})
        with patch('api.handler.application', return_value=self.auth):
            state, binder = self.begin()
            response = callback({'state': [state], 'code': ['synthetic-code'],
                'cid': ['provider-correlation'], 'extension': ['ignored'], 'expected_owner_id': ['bob']}, binder)
            self.assertEqual(response['statusCode'], 302)
            token = response['multiValueHeaders']['Set-Cookie'][-1].split(';')[0].split('=', 1)[1]
            self.assertEqual(self.auth.session(token)['user_id'], 'alice')
            self.assertEqual(callback({'state': [state], 'code': ['synthetic-code'], 'cid': ['x']}, binder)['statusCode'], 400)
            state, binder = self.begin()
            self.assertEqual(callback({'state': [state], 'code': ['one', 'two'], 'cid': ['x']}, binder)['statusCode'], 400)
            self.assertEqual(callback({'code': ['synthetic-code'], 'cid': ['x']}, binder)['statusCode'], 400)
            self.assertEqual(callback({'state': [state], 'code': ['synthetic-code'], 'error': ['access_denied']}, binder)['statusCode'], 400)
            self.assertFalse(self.store.run(lambda db: db.get('login_transactions', 'login', digest(state))).get('consumed'))
            self.provider.identity.reset_mock()
            response = callback({'state': [state], 'error': ['access_denied'],
                'error_description': ['PRIVATE PROVIDER TEXT'], 'cid': ['x']}, binder)
            self.assertEqual(response['statusCode'], 400)
            self.assertNotIn('PRIVATE PROVIDER TEXT', response['body'])
            self.provider.identity.assert_not_called()
            self.assertTrue(self.store.run(lambda db: db.get('login_transactions', 'login', digest(state)))['consumed'])


class AccountsProgramsTests(ApplicationCases, unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.prefix = 'gym_auth_'
        store = Store(self.engine, self.prefix)
        for table in store.tables.values():
            table.create(self.engine)
        self.prepare()

    def tearDown(self):
        self.engine.dispose()

    def test_provider_verifies_client_and_never_sends_token_in_url(self):
        provider = YandexProvider(self.auth.settings)
        with patch.object(provider, '_json', side_effect=[{'access_token': 'synthetic-token'},
            {'id': '101', 'client_id': 'b' * 32, 'login': 'ignored'}]) as transport:
            self.error('invalid_request', lambda: provider.identity('code', 'verifier'))
            requests = [call.args[0] for call in transport.call_args_list]
            self.assertEqual(requests[0].full_url, 'https://oauth.yandex.ru/token')
            self.assertEqual(requests[1].full_url, 'https://login.yandex.ru/info?format=json')
            self.assertEqual(requests[1].headers['Authorization'], 'OAuth synthetic-token')
            self.assertEqual(parse_qs(requests[0].data.decode())['code_verifier'], ['verifier'])
        self.assertNotIn('synthetic-client-secret', repr(self.auth.settings))
