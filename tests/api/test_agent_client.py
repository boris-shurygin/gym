"""Python-прокладка проходит настоящий MCP/прикладной слой на синтетической БД."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from api.handler import handler
import gym_agent.__main__ as client

spec = importlib.util.spec_from_file_location('delegation_helpers', Path(__file__).with_name('test_delegation.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class MemoryVault:
    def __init__(self):
        self.value = None

    def locked(self):
        return nullcontext()

    def save(self, value):
        self.value = value

    def read(self):
        return self.value


class AgentClientTests(unittest.TestCase):
    def test_python_client_reads_proposes_and_confirms_browser_publication(self):
        h = helpers.SqliteDelegationTests(methodName='runTest')
        h.setUp()
        try:
            request_id = h.begin(client_id=client.CLIENT, redirect_uri=client.CALLBACK)
            redirect = h.delegation.consent(h.h.alice, {'request_id': request_id, 'allow': True}, True)
            code = parse_qs(urlsplit(redirect['redirect_uri']).query)['code'][0]
            tokens = h.delegation.token({'grant_type': 'authorization_code', 'client_id': client.CLIENT,
                'resource': 'https://gym.example/mcp', 'redirect_uri': client.CALLBACK,
                'code': code, 'code_verifier': h.verifier})
            calls = []
            def transport(url, value, *, token=None, form=False):
                calls.append((urlsplit(url).path, value.get('method') or value.get('grant_type')))
                response = handler({'httpMethod': 'POST', 'path': urlsplit(url).path,
                    'headers': {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
                        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25',
                        **({'Authorization': 'Bearer ' + token} if token else {})},
                    'body': urlencode(value) if form else json.dumps(value)})
                self.assertIn(response['statusCode'], (200, 202))
                return json.loads(response['body']) if response['body'] else None
            with patch.object(client, 'ORIGIN', 'https://gym.example'), patch.object(client, 'request_json', side_effect=transport), patch('api.handler.application', return_value=h.auth):
                bridge = client.GymClient(MemoryVault())
                bridge.save_tokens(tokens)
                self.assertEqual(len(bridge.command()['tools']), 11)
                self.assertEqual(bridge.command('get_program')['data']['owner_id'], 'alice')
                change = bridge.command('propose_program_changes', {'request_id': str(uuid4()),
                    'expected_owner_id': 'alice', 'proposal': {'base_program_version_id': None,
                        'content': helpers.helpers.CONTENT, 'rationale': 'Проверка Python-клиента'}})
                self.assertIn(change['data']['change_id'], change['review_url'])
                bridge.command('validate_program_changes', {'change_id': change['data']['change_id'], 'expected_revision': 1})
                self.assertIsNone(bridge.command('get_program')['data']['version'])
                h.h.programs.execute('approve_and_publish', h.h.alice, h.h.approve_input(change['data']))
                published = bridge.command('get_program_change', {'change_id': change['data']['change_id']})
                self.assertEqual(published['publication']['program_version']['version'], 1)
                bridge.vault.value['expires_at'] = 0
                self.assertEqual(bridge.command('get_program')['data']['version']['version'], 1)
                self.assertIn(('/oauth/token', 'refresh_token'), calls)
                self.assertIn(('/mcp', 'notifications/initialized'), calls)
        finally:
            h.tearDown()

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI и msvcrt проверяются локально на Windows')
    def test_encrypted_vault_and_parallel_refresh(self):
        root = client.ROOT / '.local'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(root.resolve()))
            vault = client.Vault()
            vault.path = Path(directory) / 'staging.dat'
            bridge = client.GymClient(vault)
            tokens = {'token_type': 'Bearer', 'expires_in': 600, 'access_token': 'a' * 43,
                'refresh_token': 'r' * 43, 'scope': 'program:read'}
            with vault.locked():
                bridge.save_tokens(tokens)
                saved = vault.read()
                saved['expires_at'] = 0
                vault.save(saved)
            self.assertNotIn(b'"refresh_token"', vault.path.read_bytes())
            self.assertNotIn(b'r' * 43, vault.path.read_bytes())
            with patch.object(client, 'request_json', return_value=tokens) as transport:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    self.assertEqual(list(executor.map(lambda _: bridge.access(), range(2))), ['a' * 43] * 2)
                self.assertEqual(transport.call_count, 1)
            with self.assertRaises(client.ClientError):
                client.protect(b'not-encrypted', True)
