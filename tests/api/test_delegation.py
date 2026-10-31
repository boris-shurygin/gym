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
from api.delegation import Delegation, DelegatedAuth, OAuthError, SCOPES, challenge
from api.errors import ServiceError
from api.handler import handler
from api.programs import Programs
from api.store import Store

spec = importlib.util.spec_from_file_location('account_helpers', Path(__file__).with_name('test_accounts_programs.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class DelegationCases:
    def prepare_delegation(self):
        self.h = helpers.ApplicationCases()
        self.h.engine, self.h.prefix = self.engine, self.prefix
        self.h.prepare()
        self.auth = self.h.auth
        self.delegation = Delegation(self.auth)
        self.verifier = 'v' * 43

    def begin(self, scopes=SCOPES, **overrides):
        query = dict(client_id='gym-chatgpt', redirect_uri='https://chatgpt.com/connector_platform_oauth_redirect',
            response_type='code', scope=' '.join(scopes), state='client-state', resource='https://gym.example/mcp',
            code_challenge=challenge(self.verifier), code_challenge_method='S256') | overrides
        location = self.delegation.begin(query)
        return parse_qs(urlsplit(location.split('#', 1)[1]).query)['request_id'][0]

    def issue(self, scopes=SCOPES, browser=None):
        request_id = self.begin(scopes)
        result = self.delegation.consent(browser or self.h.alice, {'request_id': request_id, 'allow': True}, True)
        response = parse_qs(urlsplit(result['redirect_uri']).query)
        self.assertEqual(response['iss'], ['https://gym.example'])
        self.assertEqual(response['state'], ['client-state'])
        value = dict(grant_type='authorization_code', client_id='gym-chatgpt', code=response['code'][0],
            redirect_uri='https://chatgpt.com/connector_platform_oauth_redirect', code_verifier=self.verifier,
            resource='https://gym.example/mcp')
        return self.delegation.token(value), value

    def refresh(self, token):
        return self.delegation.token(dict(grant_type='refresh_token', client_id='gym-chatgpt',
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


class SqliteDelegationTests(DelegationCases, unittest.TestCase):
    def setUp(self):
        self.engine, self.prefix = sa.create_engine('sqlite://'), 'gym_p10_'
        for table in Store(self.engine, self.prefix).tables.values():
            table.create(self.engine)
        self.prepare_delegation()

    def tearDown(self):
        self.engine.dispose()
