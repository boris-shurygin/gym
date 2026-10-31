"""Закрытый OAuth-клиент, PKCE и отзываемые делегированные права Gym."""

import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import urlencode

from api.auth import digest, timestamp
from api.errors import ServiceError
from contracts.canonical import fingerprint

SCOPES = ('program:read', 'history:read', 'stats:read', 'program:draft', 'program:publish')
CLIENTS = {'gym-chatgpt': {'name': 'ChatGPT',
    'redirects': ['https://chatgpt.com/connector_platform_oauth_redirect']},
    'gym-python': {'name': 'Python-агент Gym', 'redirects': ['http://127.0.0.1:8765/callback']}}
OPAQUE = re.compile(r'[A-Za-z0-9_-]{43,128}')


class OAuthError(Exception):
    def __init__(self, code='invalid_request'):
        self.code = code


def exact(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= value.keys():
        raise OAuthError()


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')


class Delegation:
    def __init__(self, auth):
        self.auth, self.store = auth, auth.store
        self.origin, self.resource = auth.settings.origin, auth.settings.origin + '/mcp'

    def metadata(self, resource=False):
        if resource:
            return {'resource': self.resource, 'authorization_servers': [self.origin],
                'scopes_supported': list(SCOPES), 'bearer_methods_supported': ['header']}
        return {'issuer': self.origin, 'authorization_endpoint': self.origin + '/oauth/authorize',
            'token_endpoint': self.origin + '/oauth/token', 'revocation_endpoint': self.origin + '/oauth/revoke',
            'response_types_supported': ['code'], 'grant_types_supported': ['authorization_code', 'refresh_token'],
            'code_challenge_methods_supported': ['S256'], 'token_endpoint_auth_methods_supported': ['none'],
            'authorization_response_iss_parameter_supported': True, 'scopes_supported': list(SCOPES)}

    def begin(self, value):
        # OAuth разрешает расширения запроса; неизвестные параметры не меняют
        # проверяемые client/redirect/resource/PKCE и не входят в согласие.
        value = {key: item for key, item in value.items() if key in ('client_id', 'redirect_uri',
            'response_type', 'scope', 'state', 'resource', 'code_challenge', 'code_challenge_method')}
        exact(value, ('client_id', 'redirect_uri', 'response_type', 'scope', 'state', 'resource',
            'code_challenge', 'code_challenge_method'), ('client_id', 'redirect_uri', 'response_type',
            'state', 'resource', 'code_challenge', 'code_challenge_method'))
        client = CLIENTS.get(value['client_id'])
        scopes = value.get('scope', ' '.join(SCOPES)).split()
        if (not client or value['redirect_uri'] not in client['redirects'] or value['response_type'] != 'code'
                or value['resource'] != self.resource or value['code_challenge_method'] != 'S256'
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value['code_challenge'])
                or not 1 <= len(value['state']) <= 1024 or any(ord(c) < 32 for c in value['state'])
                or not scopes or len(scopes) != len(set(scopes)) or set(scopes) - set(SCOPES)):
            raise OAuthError()
        identifier, now = secrets.token_urlsafe(32), int(self.auth.now())
        def save(db):
            bucket = 'oauth:' + str(now // 60)
            rate = db.get('login_transactions', 'rate', bucket) or {'count': 0}
            if rate['count'] >= 60:
                raise ServiceError('rate_limited')
            db.put('login_transactions', 'rate', bucket, {'count': rate['count'] + 1, 'expires': now + 300})
            db.put('login_transactions', 'oauth', digest(identifier),
                dict(value, scopes=sorted(scopes), expires=now + 600, used=False))
        self.store.run(save)
        return '/#/connect?request_id=' + identifier

    def consent(self, token, value, approve=False):
        exact(value, ('request_id', 'allow') if approve else ('request_id',), ('request_id',))
        if not isinstance(value['request_id'], str) or not OPAQUE.fullmatch(value['request_id']):
            raise OAuthError()
        if approve and type(value.get('allow')) is not bool:
            raise OAuthError()
        code, now = secrets.token_urlsafe(32), int(self.auth.now())
        def act(db):
            principal = self.auth.principal(db, token)
            row = db.get('login_transactions', 'oauth', digest(value['request_id']))
            if not row or row['used'] or now >= row['expires']:
                raise OAuthError()
            client = CLIENTS.get(row['client_id'])
            if not client or row['redirect_uri'] not in client['redirects']:
                raise OAuthError()
            if not approve:
                return {'client_name': client['name'], 'scopes': row['scopes'], 'resource': self.resource}
            if now - principal['auth_at'] >= 600:
                raise ServiceError('session_required')
            row['used'] = True
            db.put('login_transactions', 'oauth', digest(value['request_id']), row)
            response = {'state': row['state'], 'iss': self.origin}
            if value['allow']:
                grant_id = 'gr_' + secrets.token_hex(16)
                grant = {'grant_id': grant_id, 'client_id': row['client_id'], 'client_name': client['name'],
                    'scopes': row['scopes'], 'created_at': timestamp(now), 'expires_at': timestamp(now + 90 * 86400),
                    'revoked_at': None, 'subject_hash': principal['subject_hash'], 'created': now,
                    'last_refresh': now, 'owner': principal['user_id'], 'resource': self.resource}
                db.put('program_records', principal['user_id'], 'grant:' + grant_id, grant)
                db.put('login_transactions', 'oauth-code', digest(code),
                    dict(row, owner=principal['user_id'], grant_id=grant_id, expires=now + 120, used=False))
                response['code'] = code
            else:
                response['error'] = 'access_denied'
            return {'redirect_uri': row['redirect_uri'] + '?' + urlencode(response)}
        return self.store.run(act)

    def _grant(self, db, owner, identifier):
        row = db.get('program_records', owner, 'grant:' + identifier)
        now = int(self.auth.now())
        if (not row or row['revoked_at'] or now >= row['created'] + 90 * 86400
                or now >= row['last_refresh'] + 30 * 86400 or row['resource'] != self.resource
                or row['client_id'] not in CLIENTS):
            raise ServiceError('invalid_token')
        admission = db.get('admissions', 'yandex', row['subject_hash'])
        if not admission or admission['state'] != 'active' or admission['user_id'] != owner:
            raise ServiceError('account_disabled')
        return row

    def token(self, value):
        exact(value, ('grant_type', 'client_id', 'code', 'redirect_uri', 'code_verifier', 'resource',
            'refresh_token', 'scope'), ('grant_type', 'client_id', 'resource'))
        if value['client_id'] not in CLIENTS or value['resource'] != self.resource:
            raise OAuthError('invalid_client' if value['client_id'] not in CLIENTS else 'invalid_target')
        now = int(self.auth.now())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        def exchange(db):
            kind = value['grant_type']
            if kind == 'authorization_code':
                exact(value, ('grant_type', 'client_id', 'code', 'redirect_uri', 'code_verifier', 'resource'),
                    ('code', 'redirect_uri', 'code_verifier'))
                verifier = value['code_verifier']
                if not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
                    raise OAuthError('invalid_grant')
                row = db.get('login_transactions', 'oauth-code', digest(value['code']))
                if (not row or row['used'] or now >= row['expires'] or row['client_id'] != value['client_id']
                        or row['redirect_uri'] != value['redirect_uri'] or row['resource'] != value['resource']
                        or not hmac.compare_digest(row['code_challenge'], challenge(verifier))):
                    raise OAuthError('invalid_grant')
                row['used'] = True
                db.put('login_transactions', 'oauth-code', digest(value['code']), row)
            elif kind == 'refresh_token':
                exact(value, ('grant_type', 'client_id', 'refresh_token', 'resource', 'scope'), ('refresh_token',))
                row = db.get('sessions', 'oauth-refresh', digest(value['refresh_token']))
                if not row or row['client_id'] != value['client_id'] or now >= row['expires']:
                    raise OAuthError('invalid_grant')
                if row['used']:
                    # Верни маркер, чтобы отзыв коммитился, а не откатывался с исключением.
                    grant = db.get('program_records', row['owner'], 'grant:' + row['grant_id'])
                    if grant:
                        grant['revoked_at'] = timestamp(now)
                        db.put('program_records', row['owner'], 'grant:' + row['grant_id'], grant)
                    return None
                row['used'] = True
                db.put('sessions', 'oauth-refresh', digest(value['refresh_token']), row)
            else:
                raise OAuthError('unsupported_grant_type')
            try:
                grant = self._grant(db, row['owner'], row['grant_id'])
            except ServiceError:
                raise OAuthError('invalid_grant') from None
            if 'scope' in value and set(value['scope'].split()) != set(grant['scopes']):
                raise OAuthError('invalid_scope')
            grant['last_refresh'] = now
            db.put('program_records', row['owner'], 'grant:' + row['grant_id'], grant)
            common = {'owner': row['owner'], 'grant_id': row['grant_id'], 'client_id': value['client_id'],
                'resource': self.resource}
            db.put('sessions', 'oauth-access', digest(access), dict(common, expires=now + 600))
            db.put('sessions', 'oauth-refresh', digest(refresh), dict(common, used=False,
                expires=min(now + 30 * 86400, grant['created'] + 90 * 86400)))
            return {'access_token': access, 'refresh_token': refresh, 'token_type': 'Bearer', 'expires_in': 600,
                'scope': ' '.join(grant['scopes'])}
        result = self.store.run(exchange)
        if result is None:
            raise OAuthError('invalid_grant')
        return result

    def principal(self, db, token, scope=None):
        if not isinstance(token, str) or not OPAQUE.fullmatch(token):
            raise ServiceError('invalid_token')
        row = db.get('sessions', 'oauth-access', digest(token))
        if not row or int(self.auth.now()) >= row['expires'] or row['resource'] != self.resource:
            raise ServiceError('invalid_token')
        grant = self._grant(db, row['owner'], row['grant_id'])
        if row['client_id'] != grant['client_id']:
            raise ServiceError('invalid_token')
        if scope and scope not in grant['scopes']:
            raise ServiceError('insufficient_scope')
        return {'user_id': row['owner'], 'grant_id': row['grant_id'], 'client_id': row['client_id'],
            'scopes': grant['scopes'], 'subject_hash': grant['subject_hash']}

    def grants(self, token, value, revoke=False):
        def run(db):
            owner = self.auth.principal(db, token)['user_id']
            if value.get('expected_owner_id', owner) != owner:
                raise ServiceError('account_mismatch')
            if revoke:
                key = 'request:' + value['request_id']
                request_digest = fingerprint('request', {'operation': 'revoke_grant', 'input': value})
                previous = db.get('program_records', owner, key)
                if previous:
                    if previous['digest'] != request_digest:
                        raise ServiceError('request_id_reused')
                    return previous['result']
                row = db.get('program_records', owner, 'grant:' + value['grant_id'])
                if not row:
                    raise ServiceError('not_found')
                row['revoked_at'] = row['revoked_at'] or timestamp(int(self.auth.now()))
                db.put('program_records', owner, 'grant:' + row['grant_id'], row)
                result = self.public_grant(row)
                db.put('program_records', owner, key, {'digest': request_digest, 'result': result})
                return result
            return {'items': [self.public_grant(row) for _, row in db.scan('program_records', owner, 'grant:', limit=100)]}
        return self.store.run(run)

    @staticmethod
    def public_grant(row):
        return {key: row[key] for key in ('grant_id', 'client_id', 'client_name', 'scopes',
            'created_at', 'expires_at', 'revoked_at')}

    def revoke_token(self, value):
        exact(value, ('token', 'client_id', 'token_type_hint'), ('token', 'client_id'))
        if value['client_id'] not in CLIENTS:
            raise OAuthError('invalid_client')
        def run(db):
            for kind in ('oauth-access', 'oauth-refresh'):
                row = db.get('sessions', kind, digest(value['token']))
                if row and row['client_id'] == value['client_id']:
                    grant = db.get('program_records', row['owner'], 'grant:' + row['grant_id'])
                    if grant:
                        grant['revoked_at'] = grant['revoked_at'] or timestamp(int(self.auth.now()))
                        db.put('program_records', row['owner'], 'grant:' + row['grant_id'], grant)
        self.store.run(run)


class DelegatedAuth:
    """Проверяй токен и scope повторно внутри транзакции общего сервиса."""
    def __init__(self, delegation, scope):
        self.delegation, self.scope = delegation, scope
        self.store, self.now = delegation.store, delegation.auth.now

    def principal(self, db, token, **kwargs):
        return self.delegation.principal(db, token, self.scope)
