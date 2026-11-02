"""OAuth/MCP: те же проверки выполняются на SQLite и настоящей YDB."""

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlencode
from uuid import uuid4

import sqlalchemy as sa
from api.auth import SESSION_COOKIE, AGENT_COOKIE
from api.delegation import Delegation, DelegatedAuth, OAuthError, SCOPES, DIRECT_SCOPE, challenge
from api.errors import ServiceError
from api.handler import handler
from api.programs import Programs
from api.store import Store
from contracts.validation import validate

spec = importlib.util.spec_from_file_location('account_helpers', Path(__file__).with_name('test_accounts_programs.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class DelegationCases:
    def test_mcp_schema2_draft_uses_same_approval_and_owner_checks(self):
        fixture_spec = importlib.util.spec_from_file_location('mcp_measurement_fixture', Path(__file__).with_name('test_measurements.py'))
        fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture)
        content = fixture.program2()
        issued, _ = self.issue()
        access = issued['access_token']
        version = {'X-Gym-Schema-Version': '2'}
        tools = json.loads(self.rpc(access)['body'])['result']['tools']
        proposal_tool = next(tool for tool in tools if tool['name'] == 'propose_program_changes')
        self.assertIn('ProgramContentV2', proposal_tool['inputSchema']['$defs'])
        response = self.rpc(access, 'tools/call', {'name': 'propose_program_changes', 'arguments': {
            'request_id': str(uuid4()), 'expected_owner_id': 'alice', 'proposal': {
                'base_program_version_id': None, 'content': content, 'rationale': 'Синтетические измерения'}}}, **version)
        result = json.loads(response['body'])['result']
        self.assertFalse(result['isError'])
        change = result['structuredContent']['data']
        self.assertEqual(change['proposal']['content'], content)
        self.assertIsNone(self.h.execute('get_program')['version'])
        validated = self.rpc(access, 'tools/call', {'name': 'validate_program_changes', 'arguments': {
            'change_id': change['change_id'], 'expected_revision': 1}}, **version)
        self.assertFalse(json.loads(validated['body'])['result']['isError'])
        self.h.publish(change)
        legacy = self.rpc(access, 'tools/call', {'name': 'get_program', 'arguments': {}})
        self.assertEqual(json.loads(legacy['body'])['result']['structuredContent']['error']['code'], 'upgrade_required')
        current = self.rpc(access, 'tools/call', {'name': 'get_program', 'arguments': {}}, **version)
        self.assertEqual(json.loads(current['body'])['result']['structuredContent']['data']['version']['content'], content)
        other, _ = self.issue(browser=self.h.bob)
        foreign = self.rpc(other['access_token'], 'tools/call', {'name': 'get_program_change', 'arguments': {
            'change_id': change['change_id']}}, **version)
        self.assertEqual(json.loads(foreign['body'])['result']['structuredContent']['error']['code'], 'not_found')

    def prepare_delegation(self):
        self.h = helpers.ApplicationCases()
        self.h.engine, self.h.prefix = self.engine, self.prefix
        self.h.prepare()
        self.auth = self.h.auth
        self.delegation = Delegation(self.auth)
        self.verifier = 'v' * 43

    def begin(self, scopes=SCOPES, **overrides):
        query = dict(client_id='gym-chatgpt', redirect_uri='https://chatgpt.com/connector_platform_oauth_redirect',
            response_type='code', scope=' '.join(scopes or SCOPES), state='client-state', resource='https://gym.example/mcp',
            code_challenge=challenge(self.verifier), code_challenge_method='S256') | overrides
        if scopes is None:
            query.pop('scope')
        location = self.delegation.begin(query)
        return parse_qs(urlsplit(location.split('#', 1)[1]).query)['request_id'][0]

    def issue(self, scopes=SCOPES, browser=None, *, client_id='gym-chatgpt'):
        redirect = ('http://127.0.0.1:8765/callback' if client_id == 'gym-python'
            else 'https://chatgpt.com/connector_platform_oauth_redirect')
        request_id = self.begin(scopes, client_id=client_id, redirect_uri=redirect)
        result = self.delegation.consent(browser or self.h.alice, {'request_id': request_id, 'allow': True}, True)
        response = parse_qs(urlsplit(result['redirect_uri']).query)
        self.assertEqual(response['iss'], ['https://gym.example'])
        self.assertEqual(response['state'], ['client-state'])
        value = dict(grant_type='authorization_code', client_id=client_id, code=response['code'][0],
            redirect_uri=redirect, code_verifier=self.verifier,
            resource='https://gym.example/mcp')
        return self.delegation.token(value), value

    def refresh(self, token, *, client_id='gym-chatgpt'):
        return self.delegation.token(dict(grant_type='refresh_token', client_id=client_id,
            resource='https://gym.example/mcp', refresh_token=token))

    def rpc(self, access=None, method='tools/list', params=None, **headers):
        with patch('api.handler.application', return_value=self.auth):
            return handler({'httpMethod': 'POST', 'path': '/mcp', 'headers': {
                'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                **({'Authorization': 'Bearer ' + access} if access else {}), **headers},
                'body': json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})})

    def test_code_pkce_and_resource_are_bound_and_one_use(self):
        request_id = self.begin()
        response = self.delegation.consent(self.h.alice, {'request_id': request_id, 'allow': True}, True)
        code = parse_qs(urlsplit(response['redirect_uri']).query)['code'][0]
        value = dict(grant_type='authorization_code', client_id='gym-chatgpt', code=code,
            redirect_uri='https://chatgpt.com/connector_platform_oauth_redirect', code_verifier=self.verifier,
            resource='https://gym.example/mcp')
        for key, bad in [('code_verifier', 'x' * 43), ('redirect_uri', 'https://evil.example'),
                ('client_id', 'unknown'), ('resource', 'https://other.example/mcp')]:
            with self.subTest(key=key), self.assertRaises(OAuthError):
                self.delegation.token(value | {key: bad})
        result = self.delegation.token(value)
        self.assertEqual(result['expires_in'], 600)
        with self.assertRaises(OAuthError):
            self.delegation.token(value)
        with self.assertRaises(OAuthError):
            self.delegation.consent(self.h.alice, {'request_id': request_id, 'allow': True}, True)

    def test_scopes_cookie_token_expiry_and_disabled_account(self):
        result, _ = self.issue(('program:read',))
        response = self.rpc(result['access_token'])
        tools = json.loads(response['body'])['result']['tools']
        self.assertEqual({t['name'] for t in tools}, {'get_program', 'list_exercises'})
        response = self.rpc(result['access_token'], 'tools/call', {'name': 'get_training_summary', 'arguments': {}})
        self.assertEqual(response['statusCode'], 403)
        self.assertIn('scope="stats:read"', response['headers']['WWW-Authenticate'])
        self.assertEqual(self.rpc(Cookie=SESSION_COOKIE + '=' + self.h.alice)['statusCode'], 401)
        self.assertEqual(self.rpc(self.h.alice)['statusCode'], 401)
        self.h.allow('101', 'different-owner')
        self.assertEqual(self.rpc(result['access_token'])['statusCode'], 403)

    def test_refresh_replay_revokes_new_and_old_tokens(self):
        first, _ = self.issue()
        second = self.refresh(first['refresh_token'])
        with self.assertRaises(OAuthError):
            self.refresh(first['refresh_token'])
        self.assertEqual(self.rpc(second['access_token'])['statusCode'], 401)
        self.assertEqual(self.rpc(first['access_token'])['statusCode'], 401)
        with self.assertRaises(OAuthError):
            self.refresh(second['refresh_token'])

    def test_agent_draft_requires_browser_approval_and_same_grant(self):
        issued, _ = self.issue()
        delegated = Programs(DelegatedAuth(self.delegation, 'program:draft'))
        change = delegated.execute('propose_change', issued['access_token'], {
            'request_id': str(uuid4()), 'expected_owner_id': 'alice',
            'proposal': {'base_program_version_id': None, 'content': helpers.CONTENT, 'rationale': 'Проверка целей'}})
        self.assertIsNotNone(change['creator_grant_id'])
        delegated.execute('validate_change', issued['access_token'], {'change_id': change['change_id'], 'expected_revision': 1})
        value = self.h.approve_input(change)
        publisher = Programs(DelegatedAuth(self.delegation, 'program:publish'))
        with self.assertRaises(ServiceError) as caught:
            publisher.execute('publish_approved', issued['access_token'], value | {'approval_id': 'fabricated'})
        self.assertEqual(caught.exception.code, 'approval_required')
        result = self.h.programs.execute('approve_and_publish', self.h.alice, value)
        applied = publisher.execute('publish_approved', issued['access_token'], value | {
            'request_id': str(uuid4()), 'approval_id': result['approval_id']})
        self.assertEqual(applied, result)
        other, _ = self.issue()
        with self.assertRaises(ServiceError):
            publisher.execute('publish_approved', other['access_token'], value | {
                'request_id': str(uuid4()), 'approval_id': result['approval_id']})
        self.assertEqual(self.h.execute('get_program')['version']['version'], 1)

    def test_revoke_is_owner_bound_and_immediate(self):
        result, _ = self.issue()
        grants = self.delegation.grants(self.h.alice, {})['items']
        self.assertEqual(len(grants), 1)
        self.assertEqual(self.delegation.grants(self.h.bob, {})['items'], [])
        value = {'request_id': str(uuid4()), 'expected_owner_id': 'alice', 'grant_id': grants[0]['grant_id']}
        with self.assertRaises(ServiceError):
            self.delegation.grants(self.h.bob, value, True)
        self.delegation.grants(self.h.alice, value, True)
        self.assertEqual(self.rpc(result['access_token'])['statusCode'], 401)

    def test_redirects_consent_csrf_and_browser_owner_binding(self):
        for changes in ({'redirect_uri': 'https://evil.example'}, {'code_challenge_method': 'plain'},
                {'resource': 'https://prod.example/mcp'}, {'scope': 'workouts:write'}):
            with self.assertRaises(OAuthError):
                self.begin(**changes)
        request_id = self.begin()
        with patch('api.handler.application', return_value=self.auth):
            nonce_cookie = AGENT_COOKIE + '=' + request_id
            anonymous = handler({'httpMethod': 'GET', 'path': '/oauth/consent', 'headers': {'Cookie': nonce_cookie}})
            self.assertEqual(anonymous['headers']['Location'], '/auth/login?return_to=/agent')
            page = handler({'httpMethod': 'GET', 'path': '/oauth/consent',
                'headers': {'Cookie': SESSION_COOKIE + '=' + self.h.alice + '; ' + nonce_cookie}})
            self.assertEqual(page['statusCode'], 200)
            self.assertTrue(page['headers']['Content-Type'].startswith('text/html'))
            self.assertEqual(page['headers']['Cache-Control'], 'no-store')
            self.assertNotIn('<script', page['body'])
            for origin in ('https://evil.example', 'null'):
                response = handler({'httpMethod': 'POST', 'path': '/oauth/consent',
                    'headers': {'Content-Type': 'application/x-www-form-urlencoded',
                        'Cookie': SESSION_COOKIE + '=' + self.h.alice + '; ' + AGENT_COOKIE + '=' + request_id,
                        'Origin': origin}, 'body': urlencode({'request_id': request_id, 'allow': 'yes',
                            'csrf_token': self.auth.session(self.h.alice)['csrf_token']})})
                self.assertEqual(response['statusCode'], 403)
            response = handler({'httpMethod': 'GET', 'path': '/api/v1/stats/summary',
                'headers': {'Cookie': SESSION_COOKIE + '=' + self.h.bob, 'X-Gym-Expected-Owner': 'alice'}})
            self.assertEqual(response['statusCode'], 403)
        self.assertEqual(len(self.delegation.grants(self.h.alice, {})['items']), 0)

    def test_mcp_calls_use_shared_validation_and_owner(self):
        first, _ = self.issue()
        second, _ = self.issue(browser=self.h.bob)
        response = self.rpc(first['access_token'], 'tools/call', {'name': 'get_program', 'arguments': {}})
        self.assertEqual(json.loads(response['body'])['result']['structuredContent']['data']['owner_id'], 'alice')
        response = self.rpc(second['access_token'], 'tools/call', {'name': 'get_program', 'arguments': {}})
        self.assertEqual(json.loads(response['body'])['result']['structuredContent']['data']['owner_id'], 'bob')
        response = self.rpc(first['access_token'], 'tools/call', {'name': 'propose_program_changes', 'arguments': {'approved': True}})
        self.assertTrue(json.loads(response['body'])['result']['isError'])
        response = self.rpc(first['access_token'], 'initialize', {'protocolVersion': '2025-11-25'})
        self.assertEqual(json.loads(response['body'])['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(self.rpc(first['access_token'], Origin='https://evil.example')['statusCode'], 403)
        self.h.clock += 601
        self.assertEqual(self.rpc(first['access_token'])['statusCode'], 401)


    service_error = helpers.ApplicationCases.error

    def direct_setup(self, *, browser=None, base=None, validated=True, scopes=SCOPES + (DIRECT_SCOPE,)):
        issued, _ = self.issue(scopes, browser=browser, client_id='gym-python')
        access = issued['access_token']
        owner = 'bob' if browser == self.h.bob else 'alice'
        draft = Programs(DelegatedAuth(self.delegation, 'program:draft'))
        change = draft.execute('propose_change', access, {'request_id': str(uuid4()),
            'expected_owner_id': owner, 'proposal': {'base_program_version_id': base,
                'content': helpers.CONTENT, 'rationale': 'Явный запрос владельца агенту'}})
        if validated:
            draft.execute('validate_change', access, {'change_id': change['change_id'], 'expected_revision': 1})
        publisher = Programs(DelegatedAuth(self.delegation, DIRECT_SCOPE))
        return issued, change, self.h.approve_input(change, owner), publisher

    def test_direct_scope_is_explicit_python_only_and_never_added_by_refresh(self):
        with self.assertRaises(OAuthError) as caught:
            self.begin(SCOPES + (DIRECT_SCOPE,))
        self.assertEqual(caught.exception.code, 'invalid_scope')
        old, _ = self.issue(scopes=None, client_id='gym-python')
        self.assertNotIn(DIRECT_SCOPE, old['scope'].split())
        tools = json.loads(self.rpc(old['access_token'])['body'])['result']['tools']
        self.assertNotIn('publish_program_changes', {tool['name'] for tool in tools})
        denied = self.rpc(old['access_token'], 'tools/call', {'name': 'publish_program_changes', 'arguments': {}})
        self.assertEqual(denied['statusCode'], 403)
        refreshed = self.refresh(old['refresh_token'], client_id='gym-python')
        self.assertEqual(set(refreshed['scope'].split()), set(SCOPES))
        self.assertIn(DIRECT_SCOPE, self.delegation.metadata()['scopes_supported'])

    def test_direct_publication_rpc_replay_and_immutable_versions(self):
        issued, change, value, _ = self.direct_setup()
        access = issued['access_token']
        response = self.rpc(access, 'tools/call', {'name': 'publish_program_changes', 'arguments': value},
            **{'X-Gym-Schema-Version': '2'})
        body = json.loads(response['body'])['result']
        self.assertFalse(body['isError'])
        result = body['structuredContent']['data']
        validate('DelegatedPublication', result)
        self.assertNotIn('approval_id', result)
        self.assertEqual(result['authorization'], {'kind': 'delegated_agent',
            'grant_id': change['creator_grant_id'], 'client_id': 'gym-python', 'scope': DIRECT_SCOPE})
        for request_id in (value['request_id'], str(uuid4())):
            replay = self.rpc(access, 'tools/call', {'name': 'publish_program_changes',
                'arguments': value | {'request_id': request_id}}, **{'X-Gym-Schema-Version': '2'})
            self.assertEqual(json.loads(replay['body'])['result']['structuredContent']['data'], result)
        publisher = Programs(DelegatedAuth(self.delegation, DIRECT_SCOPE))
        self.service_error('request_id_reused', lambda: publisher.execute('publish_direct', access,
            value | {'content_digest': '0' * 64}))
        self.service_error('program_conflict', lambda: self.h.programs.execute('approve_and_publish',
            self.h.alice, value | {'request_id': str(uuid4())}))
        first_version = result['program_version']
        draft = Programs(DelegatedAuth(self.delegation, 'program:draft'))
        next_change = draft.execute('propose_change', access, {'request_id': str(uuid4()),
            'expected_owner_id': 'alice', 'proposal': change['proposal'] | {
                'base_program_version_id': first_version['program_version_id'], 'rationale': 'Следующий явный запрос'}})
        draft.execute('validate_change', access, {'change_id': next_change['change_id'], 'expected_revision': 1})
        next_result = publisher.execute('publish_direct', access, self.h.approve_input(next_change))
        self.assertEqual(next_result['program_version']['version'], 2)
        saved = self.auth.store.run(lambda db: db.get('program_records', 'alice',
            'version:' + first_version['program_version_id']))
        self.assertEqual(saved, first_version)

    def test_direct_scope_does_not_require_legacy_publication_scope(self):
        issued, change, value, publisher = self.direct_setup(scopes=('program:read', 'program:draft', DIRECT_SCOPE))
        result = publisher.execute('publish_direct', issued['access_token'], value)
        self.assertEqual(result['authorization']['scope'], DIRECT_SCOPE)
        tools = json.loads(self.rpc(issued['access_token'])['body'])['result']['tools']
        self.assertNotIn('apply_program_changes', {tool['name'] for tool in tools})
        self.assertIn('publish_program_changes', {tool['name'] for tool in tools})

    def test_direct_publication_checks_validated_revision_digest_owner_and_grant(self):
        issued, change, value, publisher = self.direct_setup(validated=False)
        access = issued['access_token']
        self.service_error('validation_failed', lambda: publisher.execute('publish_direct', access, value))
        Programs(DelegatedAuth(self.delegation, 'program:draft')).execute('validate_change', access,
            {'change_id': change['change_id'], 'expected_revision': 1})
        for code, patch_value in [('revision_conflict', {'expected_revision': 2}),
                ('approval_invalid', {'content_digest': '0' * 64}),
                ('approval_invalid', {'base_program_version_id': 'wrong-base'}),
                ('account_mismatch', {'expected_owner_id': 'bob'})]:
            with self.subTest(code=code, input=patch_value):
                self.service_error(code, lambda: publisher.execute('publish_direct', access, value | patch_value))
        other, _ = self.issue(SCOPES + (DIRECT_SCOPE,), client_id='gym-python')
        self.service_error('approval_invalid', lambda: publisher.execute('publish_direct', other['access_token'], value))
        bob, _ = self.issue(SCOPES + (DIRECT_SCOPE,), browser=self.h.bob, client_id='gym-python')
        self.service_error('not_found', lambda: publisher.execute('publish_direct', bob['access_token'],
            value | {'expected_owner_id': 'bob'}))
        self.service_error('insufficient_scope', lambda: self.h.programs.execute('publish_direct', self.h.alice, value))
        self.assertIsNone(self.h.execute('get_program')['version'])

    def test_direct_stale_base_cannot_replace_newer_program(self):
        issued, change, value, publisher = self.direct_setup()
        draft = Programs(DelegatedAuth(self.delegation, 'program:draft'))
        other = draft.execute('propose_change', issued['access_token'], {'request_id': str(uuid4()),
            'expected_owner_id': 'alice', 'proposal': change['proposal'] | {'rationale': 'Другой черновик'}})
        draft.execute('validate_change', issued['access_token'], {'change_id': other['change_id'], 'expected_revision': 1})
        published = publisher.execute('publish_direct', issued['access_token'], value)
        self.service_error('program_conflict', lambda: publisher.execute('publish_direct', issued['access_token'],
            self.h.approve_input(other)))
        self.assertEqual(self.h.execute('get_program')['version'], published['program_version'])

    def test_direct_refresh_after_old_browser_auth_and_revocation(self):
        issued, change, value, publisher = self.direct_setup()
        self.h.clock += 601
        self.service_error('invalid_token', lambda: publisher.execute('publish_direct', issued['access_token'], value))
        fresh = self.refresh(issued['refresh_token'], client_id='gym-python')
        published = publisher.execute('publish_direct', fresh['access_token'], value)
        self.assertEqual(published['program_version']['version'], 1)
        self.delegation.grants(self.h.alice, {'request_id': str(uuid4()), 'expected_owner_id': 'alice',
            'grant_id': change['creator_grant_id']}, True)
        self.service_error('invalid_token', lambda: publisher.execute('publish_direct', fresh['access_token'], value))

    def test_direct_schema_and_revoke_guards_are_inside_transaction(self):
        issued, change, value, publisher = self.direct_setup()
        self.service_error('upgrade_required', lambda: publisher.execute('publish_direct', issued['access_token'],
            value, supported_schema_version=1))
        self.assertIsNone(self.h.execute('get_program')['version'])
        self.assertIsNone(self.auth.store.run(lambda db: db.get('program_records', 'alice',
            'request:' + value['request_id'])))
        self.service_error('upgrade_required', lambda: self.delegation.grants(self.h.alice, {},
            supported_schema_version=1))
        revoke = {'request_id': str(uuid4()), 'expected_owner_id': 'alice', 'grant_id': change['creator_grant_id']}
        self.service_error('upgrade_required', lambda: self.delegation.grants(self.h.alice, revoke, True,
            supported_schema_version=1))
        self.assertIsNone(self.auth.store.run(lambda db: db.get('program_records', 'alice',
            'grant:' + change['creator_grant_id']))['revoked_at'])
        self.assertIsNotNone(publisher.execute('publish_direct', issued['access_token'], value))
        args = {'name': 'get_program_change', 'arguments': {'change_id': change['change_id']}}
        legacy = self.rpc(issued['access_token'], 'tools/call', args)
        self.assertEqual(json.loads(legacy['body'])['result']['structuredContent']['error']['code'], 'upgrade_required')
        current = self.rpc(issued['access_token'], 'tools/call', args, **{'X-Gym-Schema-Version': '2'})
        self.assertEqual(json.loads(current['body'])['result']['_meta']['gym/publication']['authorization']['kind'],
            'delegated_agent')

    def test_direct_consent_names_permission_and_preserves_old_consent_text(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                identifier = self.begin(SCOPES + ((DIRECT_SCOPE,) if direct else ()),
                    client_id='gym-python', redirect_uri='http://127.0.0.1:8765/callback')
                with patch('api.handler.application', return_value=self.auth):
                    page = handler({'httpMethod': 'GET', 'path': '/oauth/consent',
                        'headers': {'Cookie': SESSION_COOKIE + '=' + self.h.alice + '; ' + AGENT_COOKIE + '=' + identifier}})
                self.assertEqual(page['statusCode'], 200)
                if direct:
                    self.assertIn('без отдельного одобрения каждой редакции', page['body'])
                    self.assertNotIn('Каждую новую программу ты отдельно одобряешь в Gym.', page['body'])
                else:
                    self.assertIn('Каждую новую программу ты отдельно одобряешь в Gym.', page['body'])
                    self.assertNotIn('Ты доверяешь этому агенту', page['body'])


class SqliteDelegationTests(DelegationCases, unittest.TestCase):
    def setUp(self):
        self.engine, self.prefix = sa.create_engine('sqlite://'), 'gym_p10_'
        for table in Store(self.engine, self.prefix).tables.values():
            table.create(self.engine)
        self.prepare_delegation()

    def tearDown(self):
        self.engine.dispose()
