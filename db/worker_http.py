"""Приватный административный HTTP worker; доступ извне проверяет IAM платформы."""

from contextvars import ContextVar
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import time
from uuid import uuid4

import sqlalchemy as sa
import ydb

from api.database import DatabaseSettings, create_engine
from contracts.canonical import parse_request
from db.migrate import MigrationBusy
from db.schema import SchemaMismatch
from db.worker import RepairRequired, Worker


DEADLINE = ContextVar('migration_deadline', default=None)
MAX_BODY = 16384


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings
    release_id: str
    prefix: str = 'gym_'
    mode: str = 'worker'
    recovery_ticket: dict | None = None
    crash_after_acquire: bool = False

    def __post_init__(self):
        if self.mode not in {'worker', 'bootstrap', 'recovery'}:
            raise ValueError('Неизвестный режим worker')
        if self.mode == 'recovery' and not self.recovery_ticket:
            raise ValueError('Recovery требует подтверждение из конфигурации')
        if self.crash_after_acquire and (self.database.database !=
                '/ru-central1/b1ghmudqihbvpnscd2fq/etn63tldguvkj5kcpi3r'
                or not self.prefix.startswith('gym_worker_probe_')):
            raise ValueError('Crash probe допустим только в синтетическом namespace staging')

    @classmethod
    def from_env(cls):
        return cls(DatabaseSettings.from_env(), os.environ['GYM_RELEASE_ID'],
                   os.environ.get('GYM_TABLE_PREFIX', 'gym_'), os.environ.get('GYM_WORKER_MODE', 'worker'),
                   parse_request(os.environ['GYM_RECOVERY_TICKET'].encode()) if os.environ.get('GYM_RECOVERY_TICKET') else None,
                   os.environ.get('GYM_CRASH_AFTER_ACQUIRE') == '1')


def engine_for(settings):
    engine = create_engine(settings.database)

    @sa.event.listens_for(engine, 'connect')
    def bound_connection(connection, record):
        connection.set_ydb_request_settings(ydb.BaseRequestSettings().with_timeout(5).with_operation_timeout(4))
        connection.set_ydb_retry_settings(ydb.RetrySettings(max_retries=0, max_session_acquire_timeout=5,
                                                          get_session_client_timeout=5))

    @sa.event.listens_for(engine, 'before_execute')
    def check_budget(connection, statement, multiparams, params, execution_options):
        if DEADLINE.get() is not None and time.monotonic() >= DEADLINE.get():
            raise TimeoutError('Бюджет шага исчерпан')
    return engine


class Application:
    def __init__(self, settings, worker):
        self.settings, self.worker = settings, worker

    def call(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Ожидается объект')
        operation = payload.get('operation')
        keys = {'operation', 'release_id', 'checksum'}
        if operation in {'step', 'status'}:
            keys.add('job_id')
        if set(payload) != keys or payload['release_id'] != self.worker.release or payload['checksum'] != self.worker.digest:
            raise ValueError('Неизвестные параметры/выпуск/миграции')
        if operation == 'status':
            return self.worker.status(payload['job_id'])
        if operation == 'bootstrap' and self.settings.mode == 'bootstrap':
            return self.worker.bootstrap()
        if operation == 'recover' and self.settings.mode == 'recovery':
            return self.worker.recover(self.settings.recovery_ticket)
        if operation == 'step' and self.settings.mode == 'worker':
            crash = (lambda: os._exit(72)) if self.settings.crash_after_acquire else None
            return self.worker.step(payload['job_id'], after_acquire=crash)
        raise ValueError('Операция недоступна в текущем режиме')


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != '/health':
                return self.reply(404, {'code': 'not_found'})
            self.reply(200, {'state': 'ready', 'release_id': app.worker.release,
                             'checksum': app.worker.digest, 'mode': app.settings.mode})

        def do_POST(self):
            request_id = uuid4().hex
            token = DEADLINE.set(time.monotonic() + 60)
            status, result = 503, {'code': 'worker_unavailable'}
            try:
                if self.path != '/v1/migrations':
                    status, result = 404, {'code': 'not_found'}
                else:
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or self.headers.get('Transfer-Encoding'):
                        raise ValueError('Некорректное обрамление')
                    size = int(lengths[0])
                    if not 0 < size <= MAX_BODY:
                        status, result = 413, {'code': 'body_too_large'}
                    else:
                        self.connection.settimeout(5)
                        raw = self.rfile.read(size)
                        if len(raw) != size:
                            raise ValueError('Неполное тело')
                        result = app.call(parse_request(raw))
                        status = 200
            except ValueError:
                status, result = 400, {'code': 'invalid_request'}
            except MigrationBusy:
                status, result = 409, {'code': 'migration_busy'}
            except (RepairRequired, SchemaMismatch):
                status, result = 409, {'code': 'repair_required'}
            except Exception:
                pass  # Не включай SQL, IAM-токены и диагностический текст SDK.
            finally:
                DEADLINE.reset(token)
            print(json.dumps({'event': 'migration_request', 'request_id': request_id,
                              'status': status, 'code': result.get('code'), 'state': result.get('state')}), flush=True)
            self.reply(status, result)
    return Handler


def main():
    logging.disable(logging.CRITICAL)
    settings = Settings.from_env()
    engine = engine_for(settings)
    app = Application(settings, Worker(engine, settings.release_id, settings.prefix))
    # Не открывай YDB и не запускай DDL при старте процесса или health.
    try:
        HTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), handler_for(app)).serve_forever()
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
