"""Покажи этап и безопасные реквизиты HTTP-отказа без токенов и тела ответа."""

from contextlib import contextmanager
import json
from pathlib import Path
import re
import time
import traceback
from urllib.error import HTTPError
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
REQUEST_IDS = ('x-request-id', 'x-server-trace-id', 'x-amz-request-id', 'x-github-request-id')


def http_details(error):
    seen = set()
    for _ in range(8):
        if id(error) in seen:
            break
        seen.add(id(error))
        if isinstance(error, HTTPError):
            result = {'http_status': error.code}
            try:
                url = urlsplit(error.geturl())
                # Никогда не выводи userinfo, query, fragment или полный URL.
                if url.hostname and re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', url.hostname):
                    result['host'] = url.hostname
                if re.fullmatch(r'/[a-zA-Z0-9._:/%-]{0,511}', url.path):
                    result['path'] = url.path
            except (TypeError, ValueError):
                pass
            headers = {key.lower(): value for key, value in (error.headers.items() if error.headers else [])}
            identifiers = {key: headers[key] for key in REQUEST_IDS if key in headers
                and isinstance(headers[key], str) and re.fullmatch(r'[a-zA-Z0-9:_.-]{1,128}', headers[key])}
            if identifiers:
                result['request_ids'] = identifiers
            return result
        error = error.__cause__ or error.__context__
        if error is None:
            break
    return {}


def emit(value):
    try:
        print(json.dumps(value, ensure_ascii=False), flush=True)
    except OSError:
        pass  # Сбой вывода не заменяет исходную ошибку и не повторяет операцию.


@contextmanager
def phase(name):
    started = time.monotonic()
    emit({'event': 'deployment_phase', 'phase': name, 'state': 'started'})
    try:
        yield
    except Exception as error:
        frames = []
        for frame in traceback.extract_tb(error.__traceback__):
            try:
                path = Path(frame.filename).resolve().relative_to(ROOT / 'infra')
            except ValueError:
                continue
            # Только имена функций/файлов и номера строк; без source и locals.
            frames.append({'file': 'infra/' + path.as_posix(), 'function': frame.name, 'line': frame.lineno})
        emit({'event': 'deployment_phase', 'phase': name, 'state': 'failed',
            'elapsed_ms': round((time.monotonic() - started) * 1000),
            'error_type': type(error).__name__, **http_details(error), 'frames': frames[-5:]})
        raise
    else:
        emit({'event': 'deployment_phase', 'phase': name, 'state': 'completed',
            'elapsed_ms': round((time.monotonic() - started) * 1000)})
