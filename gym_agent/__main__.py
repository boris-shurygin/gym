"""Вход через браузер и вызовы Gym staging/production без настройки ChatGPT."""
import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import ssl
import sys
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
import webbrowser
from contracts.canonical import parse_request, MAX_BYTES
from gym_agent.storage import ClientError, Vault, protect

ORIGIN = 'https://d5d734k3s76t42pa5rtn.764nr5vy.apigw.yandexcloud.net'
PRODUCTION_ORIGIN = 'https://d5dhqvda3eo6r4c2mgf6.764nr5vy.apigw.yandexcloud.net'
ROOT = Path(__file__).resolve().parents[1]
CLIENT = 'gym-python'
CALLBACK = 'http://127.0.0.1:8765/callback'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(url, value, *, token=None, form=False):
    context = ssl.create_default_context()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    headers = {'User-Agent': 'Gym-Python-agent', 'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'}
    if token:
        headers.update(Authorization='Bearer ' + token, Accept='application/json, text/event-stream',
            **{'MCP-Protocol-Version': '2025-11-25', 'X-Gym-Schema-Version': '2'})
    body = urlencode(value).encode() if form else json.dumps(value, ensure_ascii=False).encode()
    try:
        with build_opener(NoRedirect(), HTTPSHandler(context=context)).open(Request(url, data=body, headers=headers), timeout=30) as response:
            raw = response.read(2 * MAX_BYTES + 1)
            if len(raw) > 2 * MAX_BYTES:
                raise ClientError('response_too_large')
            return json.loads(raw) if raw else None
    except HTTPError as error:
        code, trace = 'server_http_' + str(error.code), None
        try:
            value = json.loads(error.read(16384)).get('error')
            candidate = value.get('code') if isinstance(value, dict) else value
            if isinstance(candidate, str) and re.fullmatch(r'[a-z_]{1,50}', candidate):
                code = candidate
            if isinstance(value, dict) and re.fullmatch(r'[a-f0-9]{32}', value.get('trace_id', '')):
                trace = value['trace_id']
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        raise ClientError(code, trace) from None
    except (OSError, ValueError):
        raise ClientError('server_unavailable') from None


class GymClient:
    def __init__(self, vault=None, *, environment='staging'):
        if environment not in ('staging', 'production'):
            raise ClientError('invalid_environment')
        self.environment = environment
        self.origin = ORIGIN if environment == 'staging' else PRODUCTION_ORIGIN
        self.resource = self.origin + '/mcp'
        self.vault = vault if vault is not None else Vault(environment, origin=self.origin)
        self.request_id = 0

    def save_tokens(self, value):
        if (not isinstance(value, dict) or value.get('token_type') != 'Bearer'
                or type(value.get('expires_in')) is not int or not 1 <= value['expires_in'] <= 600
                or any(not isinstance(value.get(k), str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', value[k])
                    for k in ('access_token', 'refresh_token'))):
            raise ClientError('invalid_token_response')
        self.vault.save(dict(value, resource=self.resource, client_id=CLIENT,
            expires_at=int(time.time()) + value['expires_in']))

    def access(self):
        with self.vault.locked():
            saved = self.vault.read()
            if saved.get('resource') != self.resource or saved.get('client_id') != CLIENT:
                raise ClientError('credential_environment_mismatch')
            if time.time() >= saved['expires_at'] - 30:
                value = request_json(self.origin + '/oauth/token', {'grant_type': 'refresh_token',
                    'client_id': CLIENT, 'resource': self.resource, 'refresh_token': saved['refresh_token']}, form=True)
                self.save_tokens(value)
                saved = self.vault.read()
            return saved['access_token']

    def rpc(self, method, params, access):
        self.request_id += 1
        value = request_json(self.resource, {'jsonrpc': '2.0', 'id': self.request_id, 'method': method, 'params': params}, token=access)
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or value.get('id') != self.request_id or 'error' in value or not isinstance(value.get('result'), dict):
            raise ClientError('mcp_request_failed')
        return value['result']

    def command(self, tool=None, arguments=None):
        access = self.access()
        initialized = self.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'gym-python-agent', 'version': '1.0.0'}}, access)
        if initialized.get('protocolVersion') != '2025-11-25':
            raise ClientError('unsupported_protocol')
        request_json(self.resource, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, token=access)
        if tool is None:
            return {'tools': self.rpc('tools/list', {}, access)['tools']}
        result = self.rpc('tools/call', {'name': tool, 'arguments': arguments or {}}, access)
        structured = result.get('structuredContent')
        if not isinstance(structured, dict) or type(structured.get('ok')) is not bool:
            raise ClientError('invalid_tool_response')
        meta = result.get('_meta', {})
        return dict(structured, **({'review_url': meta['gym/review_url']} if meta.get('gym/review_url') else {}),
            **({'publication': meta['gym/publication']} if meta.get('gym/publication') else {}))

    def login(self):
        self.vault.check_available()
        with self.vault.locked():
            return self._login()

    def _login(self):
        origin = self.origin
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                try:
                    pairs = parse_qsl(parsed.query, max_num_fields=12)
                except ValueError:
                    pairs = []
                value = dict(pairs)
                valid = (parsed.path == '/callback' and len(self.path) <= 8192 and len(value) == len(pairs)
                    and self.headers.get('Host') == '127.0.0.1:8765'
                    and re.fullmatch(r'[A-Za-z0-9_-]{43}', value.get('state', ''))
                    and hmac.compare_digest(value['state'], state) and value.get('iss') == origin
                    and (re.fullmatch(r'[A-Za-z0-9_-]{43,128}', value.get('code', '')) or value.get('error') == 'access_denied'))
                if valid:
                    self.server.outcome = value
                self.send_response(200 if valid else 400)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                self.wfile.write(b'Gym: browser step complete. You can close this tab.' if valid else b'Invalid callback.')
        with HTTPServer(('127.0.0.1', 8765), Callback) as server:
            server.timeout, server.outcome = 1, None
            url = self.origin + '/oauth/authorize?' + urlencode({'response_type': 'code', 'client_id': CLIENT,
                'redirect_uri': CALLBACK, 'resource': self.resource, 'state': state,
                'code_challenge_method': 'S256', 'code_challenge': challenge})
            if not webbrowser.open(url):
                raise ClientError('browser_unavailable')
            print(json.dumps({'login': 'waiting_for_browser', 'environment': self.environment}), flush=True)
            deadline = time.monotonic() + 300
            while server.outcome is None and time.monotonic() < deadline:
                server.handle_request()
            if server.outcome is None:
                raise ClientError('login_timeout')
            if server.outcome.get('error'):
                raise ClientError('authorization_denied')
            value = request_json(self.origin + '/oauth/token', {'grant_type': 'authorization_code',
                'client_id': CLIENT, 'resource': self.resource, 'redirect_uri': CALLBACK,
                'code': server.outcome['code'], 'code_verifier': verifier}, form=True)
        self.save_tokens(value)
        return {'ok': True, 'environment': self.environment, 'credentials_saved': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=('staging', 'production'), default='staging',
        help='Окружение Gym; production требует отдельного явного выбора и входа')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('login')
    commands.add_parser('status')
    tools = commands.add_parser('tools')
    tools.add_argument('--schema')
    call = commands.add_parser('call')
    call.add_argument('tool')
    call.add_argument('--input', help='JSON-файл или - для stdin; веса в целых граммах')
    args = parser.parse_args()
    try:
        client = GymClient(environment=args.environment)
        if args.command == 'login':
            result = client.login()
        elif args.command == 'status':
            result = {'environment': client.environment, 'credentials_saved': client.vault.exists()}
        elif args.command == 'tools':
            listing = client.command()['tools']
            result = {'tools': [tool for tool in listing if tool['name'] == args.schema]} if args.schema else {
                'tools': [{'name': tool['name'], 'description': tool['description']} for tool in listing]}
        else:
            raw = (sys.stdin.buffer.read(MAX_BYTES + 1) if args.input == '-' else
                Path(args.input).read_bytes() if args.input else b'{}')
            value = parse_request(raw)
            if not isinstance(value, dict):
                raise ClientError('invalid_input')
            result = client.command(args.tool, value)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result.get('ok', True) else 1
    except ClientError as error:
        print(json.dumps({'ok': False, 'error': error.code, **({'trace_id': error.trace_id} if error.trace_id else {})}), flush=True)
        return 1
    except (OSError, ValueError, KeyError):
        print(json.dumps({'ok': False, 'error': 'invalid_input_or_local_state'}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
