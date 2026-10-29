from unittest.mock import patch
import unittest
from urllib.parse import urlencode

from infra import staging_app as app, staging_target as target


class AuthSmokeTests(unittest.TestCase):
    def test_auth_smoke_checks_database_cookie_pkce_and_no_redirect_following(self):
        config = {'environment': {'GYM_YANDEX_CLIENT_ID': 'a' * 32}, 'secrets': [{'id': 's' * 20}]}
        location = 'https://oauth.yandex.ru/authorize?' + urlencode({'client_id': 'a' * 32,
            'response_type': 'code', 'scope': 'login:info', 'redirect_uri': target.ORIGIN + '/auth/callback/yandex',
            'code_challenge_method': 'S256', 'state': 's' * 43, 'code_challenge': 'c' * 43})
        headers = {'Location': location, 'Cache-Control': 'no-store',
            'Set-Cookie': '__Host-gym_login=' + 'b' * 43 + '; Path=/; Max-Age=300; Secure; HttpOnly; SameSite=Lax'}
        responses = [(302, headers, b''),
            (400, {'Cache-Control': 'no-store'}, '{"error":{"code":"invalid_request","message":"Яндекс не подтвердил вход."}}'.encode()),
            (400, {'Cache-Control': 'no-store'}, '{"error":{"code":"invalid_request","message":"Вход устарел или начат в другом браузере."}}'.encode()),
            (401, {'Cache-Control': 'no-store'}, b'{"error":{"code":"session_required"}}'),
            (400, {'Cache-Control': 'no-store'}, b'{"error":{"code":"invalid_request"}}')]
        with patch.object(app, 'configure_api', return_value=config), patch.object(app, 'raw_request', side_effect=responses) as transport:
            app.auth_smoke({})
            self.assertEqual(transport.call_count, 5)
            self.assertTrue(all(call.args[0].startswith(target.ORIGIN + '/auth/') for call in transport.call_args_list))
        for changed in (headers | {'Set-Cookie': headers['Set-Cookie'] + '; Domain=example.org'},
                        headers | {'Location': location.replace('S256', 'plain')}):
            with patch.object(app, 'configure_api', return_value=config), \
                    patch.object(app, 'raw_request', return_value=(302, changed, b'')), self.assertRaises(app.InterventionRequired):
                app.auth_smoke({})
