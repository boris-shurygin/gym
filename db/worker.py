"""Один ограниченный шаг миграции; не выполняй операции после возврата ответа."""

import hashlib
import json
import re
import time
from uuid import uuid4

import sqlalchemy as sa

from api.database import transaction
from db.migrate import Migrator, MigrationBusy, REVISION, checksum
from db.schema import ensure_table, verify_table


class RepairRequired(RuntimeError):
    pass


def control_table(prefix):
    # Валидатор префикса находится в Migrator/tables.
    return sa.Table(prefix + 'migration_requests', sa.MetaData(),
        sa.Column('job_id', sa.Text, primary_key=True, nullable=False),
        sa.Column('release_id', sa.Text), sa.Column('checksum', sa.Text),
        sa.Column('generation', sa.BigInteger), sa.Column('owner', sa.Text),
        sa.Column('checkpoint', sa.BigInteger), sa.Column('state', sa.Text),
        sa.Column('updated_at', sa.BigInteger), sa.Column('recovery_id', sa.Text))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Некорректный идентификатор')
    return value


class Worker:
    def __init__(self, engine, release_id, prefix='gym_', *, now=time.time):
        self.engine = engine
        self.release = identifier(release_id)
        self.migrator = Migrator(engine, prefix)
        self.control = control_table(prefix)
        self.digest = checksum()
        self.now = now

    def bootstrap(self):
        for table in (self.migrator.lock, self.migrator.jobs, self.control):
            ensure_table(self.engine, table)
        return {'state': 'ready', 'checksum': self.digest, 'revision': REVISION}

    def _row(self, connection, job_id):
        return connection.execute(sa.select(self.control).where(self.control.c.job_id == job_id)).mappings().one_or_none()

    def _bound(self, row):
        if row and (row['release_id'] != self.release or row['checksum'] != self.digest):
            raise ValueError('Задание связано с другим выпуском/набором миграций')

    def status(self, job_id):
        identifier(job_id)
        with self.engine.connect() as connection:
            row = self._row(connection, job_id)
            self._bound(row)
        if not row:
            return {'job_id': job_id, 'state': 'not_found'}
        if row['state'] == 'succeeded':
            self.migrator.verify()
        return {key: row[key] for key in ('job_id', 'state', 'generation', 'owner', 'checkpoint', 'recovery_id')} | {
            'release_id': self.release, 'checksum': self.digest, 'revision': REVISION}

    def step(self, job_id, *, after_acquire=None):
        identifier(job_id)
        owner = uuid4().hex
        migrator = Migrator(self.engine, self.migrator.prefix)
        migrator.owner = owner

        def acquire(connection):
            row = self._row(connection, job_id)
            self._bound(row)
            if row and row['state'] == 'repair_required':
                raise RepairRequired('Требуется административное восстановление')
            migrator._acquire(connection)
            generation = row['generation'] + 1 if row else 1
            values = dict(release_id=self.release, checksum=self.digest, generation=generation,
                          owner=owner, checkpoint=row['checkpoint'] if row else 0,
                          state='running', updated_at=int(self.now()), recovery_id=None)
            if row:
                connection.execute(self.control.update().where(self.control.c.job_id == job_id).values(**values))
            else:
                connection.execute(self.control.insert().values(job_id=job_id, **values))
            return values['checkpoint']

        checkpoint = transaction(self.engine, acquire)
        # Только тестовая инъекция процесса до try: имитирует реальный crash,
        # а не успешно освобождённую блокировку после пойманного Exception.
        if after_acquire:
            after_acquire()
        try:
            if checkpoint == 0:
                # Сейчас известна ровно одна короткая additive-ревизия.
                # Backfill/произвольные будущие цепочки здесь не исполняются.
                migrator.upgrade_locked(job_id, self.digest)
                state, checkpoint = 'needs_resume', 1
            else:
                migrator.verify()
                state, checkpoint = 'succeeded', 2
            def finish(connection):
                migrator._assert_owner(connection)
                connection.execute(self.control.update().where(self.control.c.job_id == job_id).values(
                    state=state, checkpoint=checkpoint, updated_at=int(self.now())))
                migrator._release(connection)
            transaction(self.engine, finish)
        except Exception:
            # Неопределённый DDL/commit не освобождает lease. Его состояние
            # восстанавливает администратор после остановки прежнего процесса.
            def repair(connection):
                migrator._assert_owner(connection)
                connection.execute(self.control.update().where(self.control.c.job_id == job_id).values(
                    state='repair_required', updated_at=int(self.now())))
            transaction(self.engine, repair)
            raise
        return self.status(job_id)

    def recover(self, ticket):
        """Используй только ticket из доверенной конфигурации recovery-ревизии."""
        expected = {'job_id', 'owner', 'generation', 'not_before', 'evidence_sha256'}
        if (not isinstance(ticket, dict) or set(ticket) != expected
                or type(ticket['generation']) is not int or ticket['generation'] < 1
                or type(ticket['not_before']) is not int
                or not re.fullmatch(r'[a-f0-9]{32}', ticket['owner'])
                or not re.fullmatch(r'[a-f0-9]{64}', ticket['evidence_sha256'])):
            raise ValueError('Некорректное подтверждение восстановления')
        job_id = identifier(ticket['job_id'])
        if self.now() < ticket['not_before']:
            raise RepairRequired('Остановка старого исполнителя ещё не подтверждена')
        receipt = hashlib.sha256(json.dumps(ticket, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        def recover(connection):
            row = self._row(connection, job_id)
            self._bound(row)
            if row and row['recovery_id'] == receipt:
                return
            if (not row or row['owner'] != ticket['owner'] or row['generation'] != ticket['generation']
                    or row['state'] not in {'running', 'repair_required'}):
                raise MigrationBusy('Владелец/поколение задания уже изменились')
            lock_owner = connection.execute(sa.select(self.migrator.lock.c.owner).where(
                self.migrator.lock.c.name == 'schema')).scalar_one_or_none()
            if lock_owner != ticket['owner']:
                raise MigrationBusy('Владелец блокировки уже изменился')
            connection.execute(self.control.update().where(self.control.c.job_id == job_id).values(
                state='needs_resume', recovery_id=receipt, updated_at=int(self.now())))
            connection.execute(self.migrator.lock.delete().where(self.migrator.lock.c.name == 'schema'))
        transaction(self.engine, recover)
        return self.status(job_id)
