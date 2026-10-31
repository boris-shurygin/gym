"""Приватная проверка готовых зависимостей при приёмке пакета функции."""

from importlib.metadata import version
import json
import platform
import sys


def handler(event, context=None):
    import grpc._cython.cygrpc
    import greenlet
    import jsonschema
    import rpds
    import sqlalchemy
    import ydb
    import ydb_dbapi
    import ydb_sqlalchemy
    import requests
    import charset_normalizer
    from ydb.iam import MetadataUrlCredentials
    MetadataUrlCredentials()  # Проверь optional-зависимость без запроса токена/БД.
    from api.handler import handler as http_handler
    from api.analytics import analytics_zone
    analytics_zone('UTC')  # Проверь наличие закреплённых данных времени в ZIP.
    response = http_handler({'httpMethod': 'GET', 'path': '/api/v1/health'})
    if response['statusCode'] != 200:
        raise RuntimeError('Health пакета не прошёл')
    return {'statusCode': 200, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
            'body': json.dumps({'status': 'ok', 'python': '.'.join(map(str, sys.version_info[:3])),
                'libc': platform.libc_ver(),
                'dependencies': {name: version(name) for name in
                    ['SQLAlchemy', 'ydb-sqlalchemy', 'ydb-dbapi', 'ydb', 'grpcio', 'greenlet', 'jsonschema', 'rpds-py',
                     'requests', 'certifi', 'charset-normalizer', 'urllib3', 'tzdata']}}),
            'isBase64Encoded': False}
