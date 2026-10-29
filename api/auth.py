"""Яндекс ID code/PKCE, одноразовый state и отзываемые сессии Gym."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
from uuid import uuid4

from api.errors import ServiceError

SESSION_COOKIE = '__Host-gym_session'
LOGIN_COOKIE = '__Host-gym_login'
IDLE = 7 * 86400
ABSOLUTE = 30 * 86400


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def cookie(name, value='', age=0):
    return f'{name}={value}; Path=/; Max-Age={age}; Secure; HttpOnly; SameSite=Lax'


def cookies(header):
    result = {}
    for part in header.split(';'):
        name, separator, value = part.strip().partition('=')
        if separator and name in {SESSION_COOKIE, LOGIN_COOKIE}:
            if name in result or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value):
                raise ServiceError('session_required')
            result[name] = value
    return result


@dataclass(frozen=True)
class AuthSettings:
    origin: str
    client_id: str
    client_secret: str = field(repr=False)

    def __post_init__(self):
        if not re.fullmatch(r'https://[a-z0-9.-]+(?::[0-9]+)?', self.origin):
            raise ValueError('Укажи фиксированный HTTPS origin')
        if not re.fullmatch(r'[a-f0-9]{32}', self.client_id) or not self.client_secret:
            raise ValueError('Настрой отдельное OAuth-приложение окружения')

    @classmethod
    def from_env(cls):
        return cls(os.environ['GYM_ORIGIN'], os.environ['GYM_YANDEX_CLIENT_ID'],
                   os.environ['GYM_YANDEX_CLIENT_SECRET'])

    @property
    def callback(self):
        return self.origin + '/auth/callback/yandex'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class YandexProvider:
    def __init__(self, settings):
        self.settings = settings

    def _json(self, request):
        # Фиксированные hosts, TLS с проверкой сертификатов, без повторов обмена.
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ServiceError('unavailable')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ServiceError('unavailable')
            return value

    def identity(self, code, verifier):
        try:
            token = self._json(Request('https://oauth.yandex.ru/token', data=urlencode({
                'grant_type': 'authorization_code', 'code': code, 'code_verifier': verifier,
                'client_id': self.settings.client_id, 'client_secret': self.settings.client_secret,
                'redirect_uri': self.settings.callback}).encode(), method='POST'))
            access = token.get('access_token')
            if not isinstance(access, str) or not access or len(access) > 8192 or '\n' in access or '\r' in access:
                raise ServiceError('unavailable')
            identity = self._json(Request('https://login.yandex.ru/info?format=json',
                headers={'Authorization': 'OAuth ' + access}))
            if (identity.get('client_id') != self.settings.client_id
                    or not isinstance(identity.get('id'), str)
                    or not re.fullmatch(r'[0-9]{1,64}', identity['id'])):
                raise ServiceError('invalid_request', reason='provider_identity_invalid')
            # Ни профиль, ни токен провайдера не сохраняются и не логируются.
            return identity['id']
        except ServiceError:
            raise
        except Exception:
            raise ServiceError('unavailable') from None


class Auth:
    def __init__(self, store, settings, provider=None, *, now=time.time):
        self.store, self.settings = store, settings
        self.provider, self.now = provider or YandexProvider(settings), now

    def begin(self, query, header):
        purpose, return_to = query.get('purpose', 'login'), query.get('return_to', '/app')
        if purpose not in {'login', 'reauth', 'switch'} or return_to not in {'/app', '/history', '/program', '/settings'}:
            raise ServiceError('invalid_request')
        previous = cookies(header).get(SESSION_COOKIE)
        # Всегда новый binder: конкурирующий вход в том же браузере требует
        # повторного начала, а не допускает подмену завершаемого аккаунта.
        state, binder, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        now = int(self.now())
        def create(db):
            bucket = str(now // 60)
            rate = db.get('login_transactions', 'rate', bucket) or {'count': 0}
            if rate['count'] >= 60:
                raise ServiceError('rate_limited')
            db.put('login_transactions', 'rate', bucket, {'count': rate['count'] + 1, 'expires': now + 300})
            expected = None
            if purpose == 'reauth':
                expected = self.principal(db, previous)['user_id']
            db.put('login_transactions', 'login', digest(state), {
                'binder': digest(binder), 'verifier': verifier, 'expires': now + 300,
                'purpose': purpose, 'return_to': return_to, 'expected': expected,
                'previous': digest(previous) if previous else None})
        self.store.run(create)
        import base64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        location = 'https://oauth.yandex.ru/authorize?' + urlencode({
            'response_type': 'code', 'client_id': self.settings.client_id, 'redirect_uri': self.settings.callback,
            'scope': 'login:info', 'state': state, 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'force_confirm': 'yes'})
        return location, [cookie(LOGIN_COOKIE, binder, 300)]

    def callback(self, query, header, trace_id):
        binder = cookies(header).get(LOGIN_COOKIE)
        state = query.get('state', '')
        if not binder or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', state):
            raise ServiceError('invalid_request', reason='login_binding_invalid')
        now = int(self.now())
        def claim(db):
            row = db.get('login_transactions', 'login', digest(state))
            if (not row or row['expires'] <= now or row.get('consumed')
                    or not hmac.compare_digest(row['binder'], digest(binder))):
                raise ServiceError('invalid_request', reason='login_binding_invalid')
            row['consumed'] = True
            claimed = {key: value for key, value in row.items() if key != 'verifier'}
            db.put('login_transactions', 'login', digest(state), claimed)
            return row
        # Claim фиксируется до внешнего обмена. Потеря ответа => новый вход.
        row = self.store.run(claim)
        if 'error' in query:
            raise ServiceError('invalid_request', reason='provider_login_denied')
        subject = self.provider.identity(query['code'], row['verifier'])
        subject_key = digest('yandex:' + subject)
        token = secrets.token_urlsafe(32)
        def finish(db):
            admission = db.get('admissions', 'yandex', subject_key)
            if not admission:
                db.put('login_requests', 'yandex', subject_key, {
                    'subject_hash': subject_key, 'verified_at': now, 'reference': trace_id})
                return None
            if admission['state'] != 'active':
                raise ServiceError('account_disabled')
            user = admission['user_id']
            if row['expected'] and user != row['expected']:
                raise ServiceError('account_mismatch')
            if row['previous']:
                db.delete('sessions', 'session', row['previous'])
            db.put('sessions', 'session', digest(token), {'user_id': user,
                'subject_hash': subject_key, 'created': now, 'last_seen': now, 'auth_at': now})
            return user
        if self.store.run(finish) is None:
            raise ServiceError('not_invited')
        # Используй корневой shell: Gateway не превращает неизвестные пути в SPA.
        return '/#' + row['return_to'], [cookie(LOGIN_COOKIE), cookie(SESSION_COOKIE, token, ABSOLUTE)]

    def principal(self, db, token, *, touch=True):
        if not token:
            raise ServiceError('session_required')
        session = db.get('sessions', 'session', digest(token))
        now = int(self.now())
        if not session or now >= min(session['created'] + ABSOLUTE, session['last_seen'] + IDLE):
            raise ServiceError('session_required')
        admission = db.get('admissions', 'yandex', session['subject_hash'])
        if not admission or admission['state'] != 'active' or admission['user_id'] != session['user_id']:
            raise ServiceError('account_disabled')
        if touch:
            session['last_seen'] = now
            db.put('sessions', 'session', digest(token), session)
        return session

    def session(self, token):
        def read(db):
            session = self.principal(db, token)
            profile = db.get('program_records', session['user_id'], 'profile') or {'analytics_timezone': 'UTC'}
            return {'user_id': session['user_id'], 'csrf_token': digest('gym:csrf:' + token),
                'expires_at': timestamp(min(session['created'] + ABSOLUTE, session['last_seen'] + IDLE)),
                'analytics_timezone': profile['analytics_timezone']}
        return self.store.run(read)

    def check_write(self, token, headers):
        if (headers.get('origin') != self.settings.origin or not token
                or not hmac.compare_digest(headers.get('x-csrf-token', '').encode('utf-8'), digest('gym:csrf:' + token).encode())):
            raise ServiceError('csrf_failed')

    def logout(self, token, headers):
        # Проверяй Origin и без cookie: cross-site POST с SameSite=Lax
        # не должен очистить cookie, которую браузер не отправил серверу.
        if headers.get('origin') != self.settings.origin:
            raise ServiceError('csrf_failed')
        def revoke(db):
            if token and db.get('sessions', 'session', digest(token)):
                self.check_write(token, headers)
                db.delete('sessions', 'session', digest(token))
        self.store.run(revoke)
        return [cookie(SESSION_COOKIE), cookie(LOGIN_COOKIE)]
