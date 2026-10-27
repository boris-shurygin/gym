import base64
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from api.dev import Handler
from api.handler import ROUTES, handler
from contracts.canonical import MAX_BYTES
from contracts.validation import validate


def event(method='GET', path='/api/v1/health', **extra):
    return {'httpMethod': method, 'path': path} | extra


class FunctionTests(unittest.TestCase):
    def check_error(self, response, status, code):
        self.assertEqual(response['statusCode'], status)
        payload = json.loads(response['body'])
        validate('ErrorResponse', payload)
        self.assertEqual(payload['error']['code'], code)
        self.assertEqual(response['headers']['X-Request-ID'], payload['error']['trace_id'])
        self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', response['headers'])

    def test_health_is_liveness_only(self):
        response = handler(event())
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(response['body']), {'status': 'ok'})
        self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        self.assertFalse(response['isBase64Encoded'])
        self.assertNotEqual(response['headers']['X-Request-ID'], handler(event())['headers']['X-Request-ID'])
        self.check_error(handler(event(body='{}')), 400, 'invalid_request')

    def test_all_contract_routes_are_closed(self):
        import re
        for route in ROUTES:
            path = re.sub(r'\{[a-z_]+\}', 'foreign_id', route['path'])
            with self.subTest(operation=route['operation_id']):
                if route['auth'] == 'cookie':
                    self.check_error(handler(event(route['method'].upper(), path)), 401, 'session_required')
                else:
                    self.check_error(handler(event(route['method'].upper(), path)), 503, 'unavailable')

    def test_forged_identity_does_not_open_private_route(self):
        secret = 'credential_must_not_escape'
        for credentials in [
            {'Cookie': '__Host-gym_session=' + secret},
            {'Authorization': 'Bearer ' + secret},
            {'X-Gym-User-ID': 'owner', 'X-Owner-ID': 'owner'},
        ]:
            with self.subTest(credentials=list(credentials)):
                response = handler(event('PUT', '/api/v1/profile', headers=credentials,
                                         body='{"owner_user_id":"owner","approved":true}'),
                                   {'authorizer': {'user_id': 'owner'}})
                self.check_error(response, 401, 'session_required')
                self.assertNotIn(secret, response['body'])

    def test_unknown_routes_do_not_become_spa_or_health(self):
        for method, path in [('GET', '/'), ('GET', '/api/v1/health/'), ('POST', '/api/v1/health'),
                             ('GET', '/api/v1/no-such-route'), ('GET', '/api/v1/workouts/a/b')]:
            with self.subTest(method=method, path=path):
                self.check_error(handler(event(method, path)), 404, 'not_found')

    def test_oauth_and_mcp_do_not_create_fake_accounts(self):
        for path in ['/mcp', '/oauth/token', '/.well-known/oauth-protected-resource/mcp']:
            self.check_error(handler(event('POST', path)), 503, 'unavailable')

    def test_body_limit_is_measured_in_decoded_bytes(self):
        for body, encoded in [('x' * (MAX_BYTES + 1), False),
                              ('я' * (MAX_BYTES // 2 + 1), False),
                              (base64.b64encode(b'x' * (MAX_BYTES + 1)).decode(), True)]:
            with self.subTest(encoded=encoded, length=len(body)):
                self.check_error(handler(event('POST', '/mcp', body=body, isBase64Encoded=encoded)),
                                 413, 'payload_too_large')

    def test_malformed_events_are_rejected(self):
        for value in [None, [], {}, event(httpMethod='get'), event(path='relative'), event(path='/\r\n'),
                      event(headers=[]), event(headers={'Cookie': 1}),
                      event(headers={'Cookie': 'a', 'cookie': 'b'}),
                      event(multiValueHeaders={'Cookie': ['a', 'b']}),
                      event(headers={'Cookie': 'a'}, multiValueHeaders={'Cookie': ['b']}),
                      event(headers={'Origin': 'safe\nforged'}), event(body={}),
                      event(body='???', isBase64Encoded=True), event(body='', isBase64Encoded='false')]:
            with self.subTest(value=value):
                self.check_error(handler(value), 400, 'invalid_request')

    def test_gateway_mirrored_headers_are_accepted(self):
        response = handler(event(headers={'Cookie': 'a'}, multiValueHeaders={'cookie': ['a']}))
        self.assertEqual(response['statusCode'], 200)

    def test_strict_json_boundary(self):
        for raw in [b'{"x":1,"x":2}', b'{"x":1.0}', b'{"x":NaN}', b'{"x":9007199254740992}',
                    b'\xff', ('[' * 66 + '0' + ']' * 66).encode()]:
            with self.subTest(raw=raw[:40]):
                self.check_error(handler(event('POST', '/mcp', headers={'Content-Type': 'application/json'},
                                               body=base64.b64encode(raw).decode(), isBase64Encoded=True)),
                                 400, 'invalid_request')
        self.check_error(handler(event('POST', '/mcp', headers={'Content-Type': 'application/json; charset=utf-8'},
                                      body='{"valid":true}')), 503, 'unavailable')
        self.check_error(handler(event('POST', '/mcp', body='{}')), 400, 'invalid_request')

    def test_logs_and_internal_errors_do_not_disclose_secrets(self):
        secret = 'token_private_query_or_exception'
        with self.assertLogs('gym.http', level='INFO') as logs:
            handler(event('GET', '/api/v1/profile', headers={'Cookie': secret}, body=secret))
            with patch('api.handler.Request.from_event', side_effect=RuntimeError(secret)):
                response = handler(event())
        self.check_error(response, 503, 'unavailable')
        self.assertNotIn(secret, response['body'] + '\n'.join(logs.output))
        self.assertEqual(len(logs.output), 2)


class LocalHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def test_real_http_health_and_private_denial(self):
        status, headers, body = self.request('GET', '/api/v1/health')
        self.assertEqual((status, body), (200, {'status': 'ok'}))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, _, body = self.request('GET', '/api/v1/profile', headers={'Cookie': '__Host-gym_session=forged'})
        self.assertEqual(status, 401)
        validate('ErrorResponse', body)
        self.assertEqual(body['error']['code'], 'session_required')

    def test_real_http_bad_json_and_oversized_body(self):
        status, _, body = self.request('POST', '/mcp', '{"a":1,"a":2}', {'Content-Type': 'application/json'})
        self.assertEqual((status, body['error']['code']), (400, 'invalid_request'))
        status, _, body = self.request('POST', '/mcp', headers={'Content-Length': str(MAX_BYTES + 1)})
        self.assertEqual((status, body['error']['code']), (413, 'payload_too_large'))


if __name__ == '__main__':
    unittest.main()
