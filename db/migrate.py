"""Запускай миграции отдельно от HTTP; production-исполнитель появится в P04."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from api.database import DatabaseSettings, create_engine, transaction
from db.schema import SchemaMismatch, ensure_table, tables, verify_table

ROOT = Path(__file__).resolve().parent / 'migrations'
REVISION = '0002_accounts_programs'


class MigrationBusy(RuntimeError):
    pass


def checksum():
    digest = hashlib.sha256()
    for path in sorted(ROOT.rglob('*.py')):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0')
        digest.update(path.read_bytes().replace(b'\r\n', b'\n') + b'\0')
    digest.update((ROOT.parent / 'schema.py').read_bytes().replace(b'\r\n', b'\n'))
    return digest.hexdigest()


class Migrator:
    def __init__(self, engine, prefix='gym_'):
        self.engine, self.prefix = engine, prefix
        self.lock, self.jobs, self.runtime = tables(prefix)
        self.owner = uuid4().hex

    def _acquire(self, connection):
        owner = connection.execute(sa.select(self.lock.c.owner).where(self.lock.c.name == 'schema')).scalar_one_or_none()
        if owner is not None:
            raise MigrationBusy('Другой исполнитель удерживает блокировку схемы')
        connection.execute(self.lock.insert().values(name='schema', owner=self.owner, acquired_at=int(time.time())))

    def _guard(self):
        with self.engine.connect() as connection:
            self._assert_owner(connection)

    def _assert_owner(self, connection):
        owner = connection.execute(sa.select(self.lock.c.owner).where(self.lock.c.name == 'schema')).scalar_one_or_none()
        if owner != self.owner:
            raise MigrationBusy('Исполнитель потерял блокировку схемы')

    def _release(self, connection):
        owner = connection.execute(sa.select(self.lock.c.owner).where(self.lock.c.name == 'schema')).scalar_one_or_none()
        if owner == self.owner:
            connection.execute(self.lock.delete().where(self.lock.c.name == 'schema'))

    def _record(self, job_id, state, digest):
        def write(connection):
            self._assert_owner(connection)
            row = connection.execute(sa.select(self.jobs).where(self.jobs.c.job_id == job_id)).mappings().one_or_none()
            if row:
                connection.execute(self.jobs.update().where(self.jobs.c.job_id == job_id).values(
                    state=state, updated_at=int(time.time())))
            else:
                connection.execute(self.jobs.insert().values(job_id=job_id, target=REVISION,
                    checksum=digest, state=state, updated_at=int(time.time())))
        transaction(self.engine, write)

    def verify(self):
        from api.store import application_tables
        with self.engine.connect() as connection:
            for table in application_tables(self.prefix).values():
                verify_table(connection, table)
            verify_table(connection, self.runtime)
            version = sa.Table(self.prefix + 'alembic_version', sa.MetaData(),
                               sa.Column('version_num', sa.Text, primary_key=True, nullable=False))
            verify_table(connection, version)
            if connection.execute(sa.select(version.c.version_num)).scalars().all() != [REVISION]:
                raise SchemaMismatch('Текущая ревизия схемы отличается от ожидаемой')

    def upgrade(self, job_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id):
            raise ValueError('Некорректный job_id')
        digest = checksum()
        ensure_table(self.engine, self.lock)
        ensure_table(self.engine, self.jobs)
        transaction(self.engine, self._acquire)
        try:
            return self.upgrade_locked(job_id, digest)
        finally:
            transaction(self.engine, self._release)

    def upgrade_locked(self, job_id, digest):
        """Вызови только под своей блокировкой; worker управляет её жизненным циклом."""
        self._guard()
        started = False
        try:
            with self.engine.connect() as connection:
                row = connection.execute(sa.select(self.jobs).where(self.jobs.c.job_id == job_id)).mappings().one_or_none()
            if row and (row['checksum'] != digest or row['target'] != REVISION):
                raise ValueError('job_id уже использован для другого набора миграций/ревизии')
            if row and row['state'] == 'succeeded':
                self.verify()
                return {'job_id': job_id, 'state': 'succeeded', 'revision': REVISION, 'replayed': True}
            self._record(job_id, 'running', digest)
            started = True
            config = Config()
            config.set_main_option('script_location', str(ROOT))
            config.attributes.update(engine=self.engine, prefix=self.prefix, guard=self._guard)
            command.upgrade(config, REVISION)
            self.verify()
            self._guard()
            self._record(job_id, 'succeeded', digest)
            return {'job_id': job_id, 'state': 'succeeded', 'revision': REVISION, 'replayed': False}
        except Exception:
            if started:
                self._record(job_id, 'failed', digest)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id', required=True)
    args = parser.parse_args()
    settings = DatabaseSettings.from_env()
    # Этот локальный CLI не является промышленным исполнителем миграций.
    if settings.auth != 'anonymous':
        parser.error('Этот CLI предназначен только для локальной одноразовой YDB')
    engine = create_engine(settings)
    try:
        print(json.dumps(Migrator(engine).upgrade(args.job_id)))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
