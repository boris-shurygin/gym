"""Только localhost/CI: настоящий HTTP handler и прикладной слой на временной SQLite.

Тестовый заголовок выбирает синтетическую сессию внутри fixture. В runtime Gym
этот адаптер не импортируется и не публикуется; реальные OAuth/облачные данные не нужны.
"""
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from api.handler import handler
from api.auth import SESSION_COOKIE, AGENT_COOKIE
from api.auth import digest
from api.store import Store

spec = importlib.util.spec_from_file_location('workout_test_helpers', ROOT / 'tests/api/test_workouts.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class Fixture(helpers.WorkoutCases):
    def __init__(self):
        self.engine = sa.create_engine('sqlite://')
        self.prefix = 'gym_e2e_'
        for table in Store(self.engine, self.prefix).tables.values():
            table.create(self.engine)
        self.prepare_workouts()
        self.signed_in = True
        self.user = 'alice'
        self.drop_next = False
        self.dropped = 0
        self.posts = []
        self.login_probe = False
        self.staging = False
        self.drop_publish = False
        self.publications = 0
        # Одинаковые прикладные сервисы публикуют по отдельному плану двум владельцам.
        self.publish(self.propose(token=self.bob, owner='bob'), token=self.bob, owner='bob')


fixtures = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Не печатай URL/синтетические cookies/тела.

    def do_GET(self):
        self.perform()

    def do_POST(self):
        self.perform()

    def do_PUT(self):
        self.perform()

    def perform(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/auth/update-app':
            raw = (ROOT / 'web/dist/update.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw); return
        from http.cookies import SimpleCookie
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        key = self.headers.get('X-Test-Fixture') or (cookie['gym_e2e_fixture'].value if 'gym_e2e_fixture' in cookie else None)
        fixture = fixtures.get(key)
        if self.path == '/__test/health':
            status, payload = 200, {'ok': True}
        elif self.path == '/__test/fixture' and self.command == 'POST':
            key = uuid4().hex
            fixtures[key] = Fixture()
            status, payload = 200, {'fixture': key}
        elif self.path == '/__test/status' and fixture:
            status, payload = 200, {'posts': fixture.posts, 'dropped': fixture.dropped, 'publications': fixture.publications}
        elif self.path == '/__gym/release' and fixture:
            status, payload = 200, {'purpose': 'staging-release' if fixture.staging else 'development'}
        elif self.path == '/__test/oauth-request' and fixture and self.command == 'POST':
            from api.delegation import Delegation, challenge
            location = Delegation(fixture.auth).begin({'client_id': 'gym-python',
                'redirect_uri': 'http://127.0.0.1:8765/callback',
                'response_type': 'code', 'state': 'synthetic-client-state',
                'resource': 'https://gym.example/mcp', 'code_challenge_method': 'S256',
                'code_challenge': challenge('v' * 43)})
            fixture.oauth_request = location.split('request_id=', 1)[1]
            status, payload = 200, {'location': '/oauth/consent'}
        elif self.path == '/__test/config' and fixture and self.command == 'POST':
            config = json.loads(body)
            for name in ('signed_in', 'user', 'drop_next', 'login_probe', 'staging', 'drop_publish'):
                if name in config:
                    setattr(fixture, name, config[name])
            if config.get('empty_program'):
                def empty(db):
                    for name in ('program_records', 'workout_records'):
                        table = db.tables[name]
                        db.connection.execute(table.delete().where(table.c.owner == fixture.user))
                fixture.store.run(empty)
            if config.get('legacy'):
                def legacy(db):
                    session = db.get('sessions', 'session', digest(fixture.alice))
                    session.pop('yandex_login', None)
                    db.put('sessions', 'session', digest(fixture.alice), session)
                fixture.store.run(legacy)
            status, payload = 200, {'ok': True}
        else:
            if not fixture or not fixture.signed_in:
                status, payload = 401, {'error': {'code': 'session_required'}}
            elif self.path.startswith('/auth/login') and fixture.login_probe:
                status, payload = 200, 'provider-redirect-probe'
            else:
                from urllib.parse import urlsplit, parse_qs
                user = fixture.user
                token = fixture.alice if user == 'alice' else fixture.bob
                event = {'httpMethod': self.command, 'path': urlsplit(self.path).path,
                    'multiValueQueryStringParameters': parse_qs(urlsplit(self.path).query),
                    'headers': {'Cookie': SESSION_COOKIE + '=' + token + (
                        '; ' + AGENT_COOKIE + '=' + fixture.oauth_request if getattr(fixture, 'oauth_request', None) else ''),
                        'Origin': (('https://gym.example' if self.headers.get('Origin') == 'http://127.0.0.1:4173'
                            else self.headers.get('Origin', '')) if urlsplit(self.path).path == '/oauth/consent'
                            else 'https://gym.example'),
                        'Content-Type': self.headers.get('Content-Type', ''),
                        'X-Gym-Expected-Owner': self.headers.get('X-Gym-Expected-Owner', user),
                        'X-CSRF-Token': self.headers.get('X-CSRF-Token', '')},
                    'body': body.decode('utf-8')}
                with patch('api.handler.application', return_value=fixture.auth):
                    result = handler(event)
                if result['headers'].get('Content-Type', '').startswith('text/html') or result['statusCode'] == 302:
                    raw = result['body'].encode('utf-8')
                    self.send_response(result['statusCode'])
                    for name, value in result['headers'].items():
                        self.send_header(name, value)
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers(); self.wfile.write(raw); return
                status, payload = result['statusCode'], json.loads(result['body'])
                if self.path.endswith('/approve-and-publish') and self.command == 'POST' and status == 200:
                    fixture.publications += 1
                    if fixture.drop_publish:
                        fixture.drop_publish = False
                        self.close_connection = True; self.connection.close(); return
                if self.command == 'POST' and self.path == '/api/v1/sync/operations':
                    fixture.posts.append(body.decode('utf-8'))
                    if fixture.drop_next:
                        fixture.drop_next = False
                        fixture.dropped += 1
                        self.close_connection = True
                        self.connection.close()
                        return
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Ожидаемый разрыв при offline/закрытии тестовой вкладки.


if __name__ == '__main__':
    class FixtureServer(HTTPServer):
        request_queue_size = 32
    with FixtureServer(('127.0.0.1', 4174), Handler) as server:
        print('Synthetic Gym API ready on localhost:4174', flush=True)
        server.serve_forever()
