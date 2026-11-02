"""Изоляция окружений и браузерный OAuth без сети/реальных секретов."""
from contextlib import nullcontext
import io
import json
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import gym_agent.__main__ as client


class MemoryVault:
    def __init__(self):
        self.value = None

    def locked(self):
        return nullcontext()

    def check_available(self):
        pass

    def save(self, value):
        self.value = value

    def read(self):
        return self.value


TOKENS = {'token_type': 'Bearer', 'expires_in': 600,
    'access_token': 'a' * 43, 'refresh_token': 'r' * 43}


class EnvironmentTests(unittest.TestCase):
    def test_default_stays_staging_and_production_is_explicit(self):
        with patch.object(client, 'Vault') as vault:
            staging = client.GymClient()
            vault.assert_called_with('staging', origin=client.ORIGIN)
            production = client.GymClient(environment='production')
            vault.assert_called_with('production', origin=client.PRODUCTION_ORIGIN)
        self.assertNotEqual(staging.resource, production.resource)
        with self.assertRaisesRegex(client.ClientError, 'invalid_environment'):
            client.GymClient(environment='https://untrusted.example')

    def test_wrong_environment_cannot_send_or_refresh_tokens(self):
        vault = MemoryVault()
        client.GymClient(vault).save_tokens(TOKENS)
        vault.value['expires_at'] = 0
        production = client.GymClient(vault, environment='production')
        with patch.object(client, 'request_json') as transport:
            with self.assertRaisesRegex(client.ClientError, 'credential_environment_mismatch'):
                production.command('get_program')
        transport.assert_not_called()

    def test_production_refresh_uses_only_production(self):
        vault = MemoryVault()
        production = client.GymClient(vault, environment='production')
        production.save_tokens(TOKENS)
        vault.value['expires_at'] = 0
        with patch.object(client, 'request_json', return_value=TOKENS) as transport:
            self.assertEqual(production.access(), TOKENS['access_token'])
        transport.assert_called_once_with(client.PRODUCTION_ORIGIN + '/oauth/token', {
            'grant_type': 'refresh_token', 'client_id': client.CLIENT,
            'resource': client.PRODUCTION_ORIGIN + '/mcp', 'refresh_token': TOKENS['refresh_token']}, form=True)
        self.assertGreater(vault.value['expires_at'], time.time())

    def test_storage_failure_precedes_browser_and_network(self):
        vault = MemoryVault()
        with patch.object(vault, 'check_available', side_effect=client.ClientError('secure_storage_locked')), \
                patch.object(client.webbrowser, 'open') as browser, \
                patch.object(client, 'request_json') as transport:
            with self.assertRaisesRegex(client.ClientError, 'secure_storage_locked'):
                client.GymClient(vault, environment='production').login()
        browser.assert_not_called()
        transport.assert_not_called()

    def exercise_production_login_callback(self, *, allow_program_publish=False):
        responses, browser_queries = [], []

        class Server:
            def __init__(self, address, handler):
                self.address, self.handler, self.step = address, handler, 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def handle_request(self):
                query = browser_queries[0]
                values = {'code': 'c' * 43, 'state': query['state'][0], 'iss': client.PRODUCTION_ORIGIN}
                host = '127.0.0.1:8765'
                if self.step == 0:
                    values['iss'] = client.ORIGIN
                elif self.step == 1:
                    values['state'] = 'x' * 43
                elif self.step == 2:
                    host = 'untrusted.example'
                elif self.step > 3:
                    raise AssertionError('Valid callback was not accepted')
                request = self.handler.__new__(self.handler)
                request.path = '/callback?' + urlencode(values)
                request.headers = {'Host': host}
                request.server, request.wfile = self, io.BytesIO()
                request.send_response = responses.append
                request.send_header = lambda *args: None
                request.end_headers = lambda: None
                request.do_GET()
                self.step += 1

        def open_browser(url):
            self.assertEqual(urlsplit(url).netloc, urlsplit(client.PRODUCTION_ORIGIN).netloc)
            browser_queries.append(parse_qs(urlsplit(url).query))
            return True

        vault = MemoryVault()
        with patch.object(client, 'HTTPServer', Server), \
                patch.object(client.webbrowser, 'open', side_effect=open_browser), \
                patch.object(client, 'request_json', return_value=TOKENS) as transport, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            result = client.GymClient(vault, environment='production').login(allow_program_publish=allow_program_publish)
        self.assertEqual(responses, [400, 400, 400, 200])
        self.assertEqual(result['environment'], 'production')
        self.assertTrue(result['credentials_saved'])
        self.assertEqual(browser_queries[0]['resource'], [client.PRODUCTION_ORIGIN + '/mcp'])
        self.assertEqual(browser_queries[0]['redirect_uri'], [client.CALLBACK])
        self.assertEqual(browser_queries[0]['code_challenge_method'], ['S256'])
        if allow_program_publish:
            self.assertIn('program:publish_direct', browser_queries[0]['scope'][0].split())
        else:
            self.assertNotIn('scope', browser_queries[0])
        self.assertEqual(transport.call_args.args[0], client.PRODUCTION_ORIGIN + '/oauth/token')
        self.assertEqual(vault.value['resource'], client.PRODUCTION_ORIGIN + '/mcp')
        self.assertNotIn(TOKENS['access_token'], output.getvalue())
        self.assertNotIn(TOKENS['refresh_token'], output.getvalue())

    def test_production_login_rejects_other_issuer_state_and_host(self):
        self.exercise_production_login_callback()

    def test_direct_publication_requires_explicit_login_scope_request(self):
        self.exercise_production_login_callback(allow_program_publish=True)

    def test_cli_publish_flag_is_opt_in(self):
        for flag in (False, True):
            with self.subTest(flag=flag), patch('sys.argv', ['gym_agent', 'login'] + (
                    ['--allow-program-publish'] if flag else [])), \
                    patch.object(client, 'GymClient') as bridge, patch('sys.stdout', new_callable=io.StringIO):
                bridge.return_value.login.return_value = {'ok': True}
                self.assertEqual(client.main(), 0)
                bridge.return_value.login.assert_called_once_with(allow_program_publish=flag)

    def test_cli_reports_selected_environment_without_exposing_credentials(self):
        with patch('sys.argv', ['gym_agent', '--environment', 'production', 'status']), \
                patch.object(client, 'Vault') as vault, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            vault.return_value.exists.return_value = False
            self.assertEqual(client.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {'environment': 'production', 'credentials_saved': False})


if __name__ == '__main__':
    unittest.main()
