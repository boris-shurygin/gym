"""Создай подключение явно; HTTP-обработчик не создаёт engine или схему."""

from dataclasses import dataclass
import ipaddress
import os
import re
from urllib.parse import urlsplit

import sqlalchemy as sa
import ydb


@dataclass(frozen=True)
class DatabaseSettings:
    endpoint: str
    database: str
    auth: str = 'metadata'

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {'grpc', 'grpcs'} or not parsed.hostname or not parsed.port:
            raise ValueError('Укажи grpc(s) endpoint с портом')
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
            raise ValueError('Endpoint не должен содержать credentials, путь или query')
        if not re.fullmatch(r'/[A-Za-z0-9_/-]+', self.database) or '..' in self.database:
            raise ValueError('Некорректный абсолютный путь базы')
        if self.auth not in {'anonymous', 'metadata', 'token'}:
            raise ValueError('Неизвестный способ авторизации YDB')
        local = parsed.hostname == 'localhost'
        try:
            local = local or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if (parsed.scheme == 'grpc' or self.auth == 'anonymous') and not local:
            raise ValueError('Без TLS/авторизации разрешён только loopback')

    @classmethod
    def from_env(cls):
        return cls(os.environ['GYM_YDB_ENDPOINT'], os.environ['GYM_YDB_DATABASE'],
                   os.environ.get('GYM_YDB_AUTH', 'metadata'))


def create_engine(settings, *, token=None):
    parsed = urlsplit(settings.endpoint)
    if settings.auth == 'anonymous':
        credentials = ydb.AnonymousCredentials()
    elif settings.auth == 'metadata':
        from ydb.iam import MetadataUrlCredentials
        credentials = MetadataUrlCredentials()
    else:
        if not token:
            raise ValueError('IAM-токен передай отдельно от URL')
        credentials = ydb.AccessTokenCredentials(token)
    base = sa.create_engine(
        sa.URL.create('yql+ydb', host=parsed.hostname, port=parsed.port, database=settings.database),
        connect_args={'protocol': parsed.scheme, 'credentials': credentials},
        pool_size=2, max_overflow=0, pool_timeout=10,
        hide_parameters=True, echo=False,
        _add_declare_for_yql_stmt_vars=True,
    )
    # У dialect 0.1.24 default_isolation_level фиксирован в AUTOCOMMIT.
    # OptionEngine применяет режим при каждом connect и корректно возвращает
    # соединение после временного AUTOCOMMIT для DDL; isolation_level аргумент
    # create_engine вызывает assertion SQLAlchemy при таком сбросе режима.
    return base.execution_options(isolation_level='SERIALIZABLE')


def transaction(engine, operation, *, max_attempts=4):
    """Повтори весь callback только после подтверждённого abort, без внешних действий."""
    if not 1 <= max_attempts <= 10:
        raise ValueError('Число попыток должно быть от 1 до 10')
    for attempt in range(max_attempts):
        try:
            with engine.begin() as connection:
                return operation(connection)
        except sa.exc.DBAPIError as error:
            original = getattr(error.orig, 'original_error', None)
            if not isinstance(original, ydb.Aborted) or attempt + 1 == max_attempts:
                # Неопределённый commit не является основанием повторять запись.
                raise
