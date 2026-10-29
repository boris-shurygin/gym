"""Адаптер HTTP-события Cloud Functions без облачных побочных эффектов."""

import base64
import binascii
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from uuid import uuid4
from functools import lru_cache
import os

from jsonschema import ValidationError
from contracts.validation import validate
from api.errors import ServiceError
from api.auth import Auth, AuthSettings, cookies, SESSION_COOKIE
from api.programs import Programs

from contracts.canonical import MAX_BYTES, canonical_bytes, parse_request

ROOT = Path(__file__).resolve().parents[1] / 'contracts'
ROUTES = json.loads((ROOT / 'bindings.json').read_text(encoding='utf-8'))['routes']
ERRORS = json.loads((ROOT / 'errors.json').read_text(encoding='utf-8'))
LOGGER = logging.getLogger('gym.http')
MESSAGES = {
    'invalid_request': 'Некорректный HTTP-запрос.',
    'payload_too_large': 'Тело запроса превышает 1 MiB.',
    'session_required': 'Требуется действующая сессия Gym.',
    'not_found': 'Маршрут не найден.',
    'unavailable': 'Сервис пока недоступен.',
    'not_invited': 'Доступ пока не предоставлен. Передай владельцу Gym код trace_id этой подтверждённой заявки.',
    'account_disabled': 'Доступ к аккаунту закрыт. Локальные записи сохранены.',
    'csrf_failed': 'Проверка действия не пройдена. Обнови сессию.',
}
SENSITIVE_HEADERS = {'authorization', 'cookie', 'origin', 'x-csrf-token'}
PATTERNS = [
    (route, re.compile(re.sub(r'\{[a-z_]+\}', r'[A-Za-z0-9_-]+', route['path'])))
    for route in ROUTES
]


class RequestError(ServiceError):
    def __init__(self, code='invalid_request'):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    query: dict[str, str]

    @classmethod
    def from_event(cls, event):
        if not isinstance(event, dict):
            raise RequestError()
        method, path = event.get('httpMethod'), event.get('path')
        if not isinstance(method, str) or not re.fullmatch(r'[A-Z]{1,16}', method):
            raise RequestError()
        if not isinstance(path, str) or not path.startswith('/') or len(path) > 4096:
            raise RequestError()
        if any(ord(c) < 32 for c in path) or '?' in path or '#' in path:
            raise RequestError()

        headers = {}
        for field, multiple in [('headers', False), ('multiValueHeaders', True)]:
            source = event.get(field)
            if source is None:
                source = {}
            if not isinstance(source, dict):
                raise RequestError()
            seen = set()
            for key, value in source.items():
                if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9!#$%&\'*+.^_`|~-]+', key):
                    raise RequestError()
                key = key.lower()
                if key in seen:
                    raise RequestError()
                seen.add(key)
                values = value if multiple else [value]
                if not isinstance(values, list) or not values or any(
                    not isinstance(v, str) or '\r' in v or '\n' in v for v in values
                ):
                    raise RequestError()
                if key in SENSITIVE_HEADERS and len(values) != 1:
                    raise RequestError()
                joined = ', '.join(values)
                if multiple and key in SENSITIVE_HEADERS and key in headers and headers[key] != joined:
                    raise RequestError()
                headers[key] = joined

        body = event.get('body')
        body = '' if body is None else body
        encoded = event.get('isBase64Encoded', False)
        if not isinstance(body, str) or type(encoded) is not bool:
            raise RequestError()
        # Проверка длины до выделения памяти для декодированного тела.
        if len(body) > (4 * ((MAX_BYTES + 2) // 3) if encoded else MAX_BYTES):
            raise RequestError('payload_too_large')
        try:
            raw = base64.b64decode(body, validate=True) if encoded else body.encode('utf-8', errors='strict')
        except (ValueError, UnicodeError, binascii.Error) as error:
            raise RequestError() from error
        if len(raw) > MAX_BYTES:
            raise RequestError('payload_too_large')
        query = {}
        for field, multiple in [('queryStringParameters', False), ('multiValueQueryStringParameters', True)]:
            source = event.get(field) or {}
            if not isinstance(source, dict):
                raise RequestError()
            for key, value in source.items():
                values = value if multiple else [value]
                if (not isinstance(key, str) or not isinstance(values, list) or len(values) != 1
                        or not isinstance(values[0], str) or len(values[0]) > 4096
                        or key in query and query[key] != values[0]):
                    raise RequestError()
                query[key] = values[0]
        return cls(method, path, headers, raw, query)

    def check_json(self):
        if self.body:
            content_type = self.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json':
                raise RequestError()
            try:
                parse_request(self.body)
            except ValueError as error:
                raise RequestError() from error


def _response(status, value, trace_id):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
            'X-Request-ID': trace_id,
        },
        'body': canonical_bytes(value).decode('utf-8'),
        'isBase64Encoded': False,
    }


def _error(code, trace_id):
    rule = ERRORS[code]
    return _response(rule['http_status'], {'error': {
        'code': code, 'message': MESSAGES.get(code, 'Запрос отклонён. Проверь действие и повтори.'), 'trace_id': trace_id,
        'retryable': rule['retryable'], 'action': rule['action'],
        'retry_after_seconds': None, 'field_errors': [], 'receipt': None,
    }}, trace_id)


@lru_cache(maxsize=1)
def application():
    if not os.environ.get('GYM_YANDEX_CLIENT_ID'):
        return None
    from api.database import DatabaseSettings, create_engine
    from api.store import Store
    return Auth(Store(create_engine(DatabaseSettings.from_env())), AuthSettings.from_env())


PROGRAM_OPERATIONS = {'get_profile', 'set_profile', 'get_program', 'propose_change', 'get_change',
    'replace_change', 'validate_change', 'approve_and_publish', 'publish_approved'}


def dispatch(request, route, trace_id):
    auth = application()
    operation = route['operation_id']
    token = cookies(request.headers.get('cookie', '')).get(SESSION_COOKIE)
    if auth is None:
        raise RequestError('session_required' if route['auth'] == 'cookie' else 'unavailable')
    if operation == 'auth_logout':
        request.check_json()
        if request.query or request.body and parse_request(request.body) != {}:
            raise RequestError()
        result = _response(204, None, trace_id)
        result['body'] = ''
        result['multiValueHeaders'] = {'Set-Cookie': auth.logout(token, request.headers)}
        return result
    if route['auth'] == 'cookie':
        # Не доверяй claims/owner из контекста Gateway. Проверяй состояние
        # аккаунта также внутри транзакции каждой прикладной операции.
        auth.session(token)
        if request.method not in {'GET', 'HEAD'}:
            auth.check_write(token, request.headers)
    request.check_json()
    if request.method == 'GET':
        if request.body:
            raise RequestError()
        value = dict(request.query)
    else:
        if request.query:
            raise RequestError()
        value = parse_request(request.body) if request.body else {}
        if not isinstance(value, dict):
            raise RequestError()
    path_names = re.findall(r'\{([a-z_]+)\}', route['path'])
    if path_names:
        pattern = re.sub(r'\{[a-z_]+\}', r'([A-Za-z0-9_-]+)', route['path'])
        for name, path_value in zip(path_names, re.fullmatch(pattern, request.path).groups()):
            if name in value and value[name] != path_value:
                raise RequestError()
            value[name] = path_value
    try:
        validate(route['input'], value)
    except (ValidationError, ValueError):
        raise RequestError('invalid_request') from None
    if operation in {'auth_login', 'auth_callback'}:
        location, set_cookies = (auth.begin(value, request.headers.get('cookie', '')) if operation == 'auth_login'
            else auth.callback(value, request.headers.get('cookie', ''), trace_id))
        response = _response(302, None, trace_id)
        response['body'] = ''
        response['headers']['Location'] = location
        response['multiValueHeaders'] = {'Set-Cookie': set_cookies}
        return response
    if operation == 'auth_session':
        result = auth.session(token)
    elif operation in PROGRAM_OPERATIONS:
        try:
            result = Programs(auth).execute(operation, token, value)
        except (ValidationError, ValueError):
            raise RequestError('validation_failed') from None
    else:
        raise RequestError('unavailable')
    validate(route['output'], result)
    return _response(route['success_status'], result, trace_id)


def handler(event, context=None):
    """Не принимай owner/claims из тела, заголовков или контекста за личность Gym."""
    trace_id = uuid4().hex
    method = 'UNKNOWN'
    try:
        request = Request.from_event(event)
        method = request.method
        if request.method == 'GET' and request.path == '/api/v1/health':
            if request.body:
                raise RequestError()
            response = _response(200, {'status': 'ok'}, trace_id)
        else:
            route = next((route for route, pattern in PATTERNS
                          if route['method'].upper() == request.method and pattern.fullmatch(request.path)), None)
            if route:
                response = dispatch(request, route, trace_id)
            elif request.path == '/mcp' or request.path.startswith(('/oauth/', '/.well-known/')):
                request.check_json()
                raise RequestError('unavailable')
            else:
                raise RequestError('not_found')
    except ServiceError as error:
        response = _error(error.code, trace_id)
    except Exception:
        # Не выводи исключение: оно может включать тело/credentials запроса.
        response = _error('unavailable', trace_id)
    LOGGER.info(json.dumps({'event': 'request_completed', 'trace_id': trace_id,
                            'method': method, 'status': response['statusCode']}))
    return response
