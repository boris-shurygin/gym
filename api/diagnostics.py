"""Ограниченная диагностика: только фиксированные поля, без данных тренировок."""
import json
import os
import threading
import time
from datetime import datetime
from urllib.request import Request, build_opener, HTTPRedirectHandler

from api.errors import ServiceError

CODES = {'storage', 'network', 'timeout', 'server', 'unavailable', 'unexpected', 'update', None}
NAMES = {'app_open', 'local_save_failed', 'sync_failed', 'sync_completed', 'api_request_slow', 'sw_update_failed', 'page_error'}
_lock = threading.Lock()
_budget = [0, 0]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def export(records):
    """Заверши OTLP внутри вызова; никакого фонового потока или повторов."""
    key = os.environ.get('GYM_MONIUM_API_KEY')
    project = os.environ.get('GYM_MONIUM_PROJECT')
    if not key or not project or not records:
        return False
    # Верхняя граница для каждого экземпляра, включая поток ошибочных запросов.
    with _lock:
        minute = int(time.time()) // 60
        if _budget[0] != minute:
            _budget[:] = [minute, 0]
        if _budget[1] >= 60:
            return False
        _budget[1] += 1
    resource = {'attributes': [
        {'key': 'service.name', 'value': {'stringValue': 'gym-api'}},
        {'key': 'deployment.name', 'value': {'stringValue': os.environ.get('GYM_ENVIRONMENT', 'local')}},
        {'key': 'service.version', 'value': {'stringValue': os.environ.get('GYM_RELEASE_ID', 'local')}},
    ]}
    logs = [{'timeUnixNano': str(time.time_ns()), 'severityText': 'INFO',
             'body': {'stringValue': json.dumps(record, separators=(',', ':'))}} for record in records[:20]]
    data = json.dumps({'resourceLogs': [{'resource': resource, 'scopeLogs': [{'logRecords': logs}]}]}).encode()
    try:
        request = Request('https://ingest.monium.yandex.cloud/otlp/v1/logs', data=data,
            headers={'Authorization': 'Api-Key ' + key, 'x-monium-project': project,
                     'x-monium-cluster': os.environ.get('GYM_ENVIRONMENT', 'local'),
                     'x-monium-service': 'gym-api', 'Content-Type': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=0.7) as response:
            raw = response.read(16385)
            if response.status != 200 or len(raw) > 16384:
                return False
            value = json.loads(raw) if raw else {}
            return int(value.get('partialSuccess', {}).get('rejectedLogRecords', 0)) == 0
    except Exception:
        # Ошибки экспортёра нельзя рекурсивно журналировать или возвращать пользователю.
        return False


def request_completed(route, method, status, duration_ms, trace_id, error_code=None):
    # Входные поля задаёт адаптер, а не заголовки/URL/тело пользователя.
    return export([{'event': 'request_completed', 'route': route, 'method': method,
        'status': status, 'duration_ms': min(60000, max(0, duration_ms)),
        'trace_id': trace_id, 'error_code': error_code}])


def receive(auth, token, value):
    if len(json.dumps(value).encode()) > 16384:
        raise ServiceError('payload_too_large')
    if any(event['code'] not in CODES or event['name'] not in NAMES for event in value['events']):
        raise ServiceError('invalid_request')
    now = int(auth.now())
    def admit(db):
        identity = auth.principal(db, token, touch=False)
        if identity['user_id'] != value['expected_owner_id']:
            raise ServiceError('account_mismatch')
        # Одна перезаписываемая запись на владельца вместо бесконечной истории квот.
        rate = db.get('program_records', identity['user_id'], 'telemetry_rate') or {}
        count = rate.get('count', 0) if rate.get('minute') == now // 60 else 0
        if count >= 2:
            raise ServiceError('rate_limited')
        db.put('program_records', identity['user_id'], 'telemetry_rate', {'minute': now // 60, 'count': count + 1})
    auth.store.run(admit)
    # event_id, owner, клиентские timestamp и произвольный текст не уходят в Monium.
    records = [{'event': event['name'], 'code': event['code'], 'duration_ms': event['duration_ms'],
                'count': event['count']} for event in value['events']
               if 0 <= now - datetime.fromisoformat(event['at'].replace('Z', '+00:00')).timestamp() <= 86400]
    if value.get('app_version'):
        records = [record | {'app_version': value['app_version']} for record in records]
    accepted = len(records) if export(records) else 0
    return {'accepted': accepted, 'dropped': len(value['events']) - accepted}
