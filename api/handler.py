"""Адаптер HTTP-события Cloud Functions без облачных побочных эффектов."""

import base64
import binascii
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from uuid import uuid4

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
}
SENSITIVE_HEADERS = {'authorization', 'cookie', 'origin', 'x-csrf-token'}
PATTERNS = [
    (route, re.compile(re.sub(r'\{[a-z_]+\}', r'[A-Za-z0-9_-]+', route['path'])))
    for route in ROUTES
]


class RequestError(Exception):
    def __init__(self, code='invalid_request'):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

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
        return cls(method, path, headers, raw)

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
            'X-Request-ID': trace_id,
        },
        'body': canonical_bytes(value).decode('utf-8'),
        'isBase64Encoded': False,
    }


def _error(code, trace_id):
    rule = ERRORS[code]
    return _response(rule['http_status'], {'error': {
        'code': code, 'message': MESSAGES[code], 'trace_id': trace_id,
        'retryable': rule['retryable'], 'action': rule['action'],
        'retry_after_seconds': None, 'field_errors': [], 'receipt': None,
    }}, trace_id)


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
            if route and route['auth'] == 'cookie':
                # Настоящий resolver сессий появится в P06. Любые присланные
                # credentials сейчас отвергаются, пользователь не создаётся.
                raise RequestError('session_required')
            if route or request.path == '/mcp' or request.path.startswith(('/oauth/', '/.well-known/')):
                request.check_json()
                raise RequestError('unavailable')
            raise RequestError('not_found')
    except RequestError as error:
        response = _error(error.code, trace_id)
    except Exception:
        # Не выводи исключение: оно может включать тело/credentials запроса.
        response = _error('unavailable', trace_id)
    LOGGER.info(json.dumps({'event': 'request_completed', 'trace_id': trace_id,
                            'method': method, 'status': response['statusCode']}))
    return response
