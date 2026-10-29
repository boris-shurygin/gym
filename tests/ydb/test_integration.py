"""Запусти на настоящей YDB; отсутствие подключения является ошибкой, не skip."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
import ydb
import ydb_dbapi

from api.database import DatabaseSettings, create_engine, transaction
from db.migrate import Migrator, MigrationBusy, REVISION
from db.schema import SchemaMismatch, ensure_table, tables
from db.schema import verify_table


class YdbIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = DatabaseSettings.from_env()
        if settings.auth != 'anonymous':
            if os.environ.get('GITHUB_ACTIONS') or os.environ.get('GYM_YDB_TEST_TARGET') != 'staging':
                raise RuntimeError('Облачная YDB запрещена в CI и без явного staging-test')
            if settings.database != '/ru-central1/b1ghmudqihbvpnscd2fq/etn63tldguvkj5kcpi3r':
                raise RuntimeError('Для облачного теста разрешена только настроенная staging YDB')
        cls.engine = create_engine(settings, token=os.environ.get('GYM_YDB_TOKEN'))
        if settings.auth != 'anonymous':
            # Serverless ограничивает частоту DDL; локальный движок CI этого
            # ограничения не имеет. Не оставляй тестовые таблицы из-за quota.
            last_ddl = [0.0]
            @sa.event.listens_for(cls.engine, 'before_execute')
            def pace_ddl(connection, statement, multiparams, params, execution_options):
                if isinstance(statement, sa.schema.DDLElement):
                    time.sleep(max(0, 1.5 - (time.monotonic() - last_ddl[0])))
                    last_ddl[0] = time.monotonic()
        cls.prefix = 'gym_it_' + uuid4().hex[:12] + '_'
        cls.prefixes = {cls.prefix}
        metadata = sa.MetaData()
        cls.values = sa.Table(cls.prefix + 'values', metadata,
                             sa.Column('owner', sa.Text, primary_key=True, nullable=False),
                             sa.Column('id', sa.Text, primary_key=True, nullable=False),
                             sa.Column('value', sa.BigInteger), sa.Column('note', sa.Text),
                             sa.Column('recorded_at', sa.DateTime))
        cls.receipts = sa.Table(cls.prefix + 'receipts', metadata,
                               sa.Column('owner', sa.Text, primary_key=True, nullable=False),
                               sa.Column('id', sa.Text, primary_key=True, nullable=False),
                               sa.Column('digest', sa.Text))
        try:
            for table in (cls.values, cls.receipts):
                ensure_table(cls.engine, table)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
                # Префикс создан этим процессом, не читается из переменных среды.
                for name in sa.inspect(connection).get_table_names():
                    if any(name.startswith(prefix) for prefix in cls.prefixes):
                        sa.Table(name, sa.MetaData()).drop(connection)
        finally:
            cls.engine.dispose()

    def migration(self, label):
        prefix = self.prefix + label + '_'
        self.prefixes.add(prefix)
        return Migrator(self.engine, prefix)

    def row(self, key, owner='alice'):
        with self.engine.connect() as connection:
            return connection.execute(sa.select(self.values).where(
                self.values.c.owner == owner, self.values.c.id == key)).mappings().one_or_none()

    def test_serializable_mode_and_integer_unicode_null_timestamp(self):
        with self.engine.begin() as connection:
            self.assertEqual(connection.get_isolation_level(), 'SERIALIZABLE')
            connection.execute(self.values.insert().values(owner='alice', id='types', value=9_007_199_254_740_991,
                note='Жим — 75 кг', recorded_at=datetime(2026, 10, 27, 8, 15, 0)))
            connection.execute(self.values.insert().values(owner='bob', id='types', value=-9_007_199_254_740_991,
                note=None, recorded_at=None))
        self.assertEqual(self.row('types')['value'], 9_007_199_254_740_991)
        self.assertEqual(self.row('types')['note'], 'Жим — 75 кг')
        self.assertEqual(self.row('types')['recorded_at'], datetime(2026, 10, 27, 8, 15, 0))
        self.assertEqual(self.row('types', 'bob')['value'], -9_007_199_254_740_991)
        self.assertIsNone(self.row('types', 'bob')['note'])

    def test_two_table_write_is_rolled_back_together(self):
        def write(connection):
            connection.execute(self.values.insert().values(owner='alice', id='rollback', value=10))
            connection.execute(self.receipts.insert().values(owner='alice', id='rollback', digest='hash'))
            raise ValueError('Остановись до commit')
        with self.assertRaises(ValueError):
            transaction(self.engine, write)
        self.assertIsNone(self.row('rollback'))
        with self.engine.connect() as connection:
            self.assertIsNone(connection.execute(sa.select(self.receipts).where(self.receipts.c.id == 'rollback')).first())

    def test_repeated_operation_reads_its_receipt_and_rejects_changed_content(self):
        def write(digest):
            def apply(connection):
                row = connection.execute(sa.select(self.receipts).where(
                    self.receipts.c.owner == 'alice', self.receipts.c.id == 'repeat')).mappings().one_or_none()
                if row:
                    if row['digest'] != digest:
                        raise ValueError('Повтор с другим содержимым')
                    return 'replayed'
                connection.execute(self.values.insert().values(owner='alice', id='repeat', value=12))
                connection.execute(self.receipts.insert().values(owner='alice', id='repeat', digest=digest))
                return 'applied'
            return transaction(self.engine, apply)
        self.assertEqual(write('same'), 'applied')
        self.assertEqual(write('same'), 'replayed')
        with self.assertRaises(ValueError):
            write('changed')
        self.assertEqual(self.row('repeat')['value'], 12)

    def test_concurrent_update_retries_whole_aborted_transaction(self):
        transaction(self.engine, lambda c: c.execute(self.values.insert().values(owner='alice', id='counter', value=0)))
        barrier = threading.Barrier(2, timeout=20)
        attempts = []
        def worker():
            calls = 0
            def increment(connection):
                nonlocal calls
                calls += 1
                value = connection.execute(sa.select(self.values.c.value).where(
                    self.values.c.owner == 'alice', self.values.c.id == 'counter')).scalar_one()
                if calls == 1:
                    barrier.wait()
                connection.execute(self.values.update().where(self.values.c.owner == 'alice',
                    self.values.c.id == 'counter').values(value=value + 1))
            transaction(self.engine, increment)
            attempts.append(calls)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(self.row('counter')['value'], 2)
        self.assertGreaterEqual(sum(attempts), 3)

    def test_lost_commit_ack_does_not_replay_callback(self):
        real_commit = self.engine.dialect.do_commit
        calls = 0
        def commit_without_ack(connection):
            real_commit(connection)
            raise ydb_dbapi.OperationalError('Неопределённый результат commit', original_error=ydb.Unavailable('lost ack'))
        def write(connection):
            nonlocal calls
            calls += 1
            connection.execute(self.values.insert().values(owner='alice', id='lost-ack', value=1))
        with patch.object(self.engine.dialect, 'do_commit', side_effect=commit_without_ack):
            with self.assertRaises(sa.exc.DBAPIError):
                transaction(self.engine, write)
        self.assertEqual(calls, 1)
        self.assertEqual(self.row('lost-ack')['value'], 1)

    def test_migrate_empty_repeat_and_existing_data(self):
        migrator = self.migration('fresh')
        self.assertFalse(migrator.upgrade('initial')['replayed'])
        transaction(self.engine, lambda c: c.execute(migrator.runtime.insert().values(key='retain', value='old')))
        self.assertTrue(migrator.upgrade('initial')['replayed'])
        self.assertEqual(migrator.upgrade('second')['revision'], REVISION)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(sa.select(migrator.runtime.c.value)).scalar_one(), 'old')

    def test_resume_after_partial_ddl(self):
        migrator = self.migration('partial')
        ensure_table(self.engine, migrator.runtime)
        self.assertEqual(migrator.upgrade('partial-ddl')['state'], 'succeeded')

    def test_upgrade_from_legacy_0001_preserves_version_table_and_data(self):
        migrator = self.migration('legacy')
        legacy = sa.Table(migrator.prefix + 'alembic_version', sa.MetaData(),
            sa.Column('version_num', sa.Text, primary_key=True, nullable=False))
        ensure_table(self.engine, legacy)
        ensure_table(self.engine, migrator.runtime)
        def seed(connection):
            connection.execute(legacy.insert().values(version_num='0001_runtime_metadata'))
            connection.execute(migrator.runtime.insert().values(key='retain', value='до P06'))
        transaction(self.engine, seed)
        self.assertEqual(migrator.upgrade('legacy-upgrade')['revision'], REVISION)
        migrator.verify()
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(sa.select(legacy.c.version_num)).scalar_one(), '0001_runtime_metadata')
            self.assertEqual(connection.execute(sa.select(migrator.runtime.c.value)).scalar_one(), 'до P06')
        self.assertTrue(migrator.upgrade('legacy-upgrade')['replayed'])

    def test_unknown_legacy_revision_is_not_replaced_or_upgraded(self):
        migrator = self.migration('legacy_unknown')
        legacy = sa.Table(migrator.prefix + 'alembic_version', sa.MetaData(),
            sa.Column('version_num', sa.Text, primary_key=True, nullable=False))
        ensure_table(self.engine, legacy)
        transaction(self.engine, lambda c: c.execute(legacy.insert().values(version_num='unknown_revision')))
        with self.assertRaises(SchemaMismatch):
            migrator.upgrade('unknown-legacy')
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(sa.select(legacy.c.version_num)).scalar_one(), 'unknown_revision')
            self.assertFalse(sa.inspect(connection).has_table(migrator.prefix + 'schema_version'))

    def test_wrong_schema_is_not_stamped_then_can_be_repaired(self):
        migrator = self.migration('wrong')
        wrong = sa.Table(migrator.runtime.name, sa.MetaData(),
                         sa.Column('key', sa.Text, primary_key=True, nullable=False),
                         sa.Column('value', sa.BigInteger))
        ensure_table(self.engine, wrong)
        with self.assertRaises(SchemaMismatch):
            migrator.upgrade('repairable')
        with self.engine.connect() as connection:
            version = sa.table(migrator.prefix + 'schema_version', sa.column('version_num'))
            self.assertEqual(connection.execute(sa.select(version)).all(), [])
            self.assertEqual(connection.execute(sa.select(migrator.jobs.c.state)).scalar_one(), 'failed')
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            wrong.drop(connection)
        self.assertEqual(migrator.upgrade('repairable')['state'], 'succeeded')

    def test_lock_prevents_parallel_migration(self):
        holder = self.migration('locked')
        ensure_table(self.engine, holder.lock)
        transaction(self.engine, holder._acquire)
        try:
            with self.assertRaises(MigrationBusy):
                Migrator(self.engine, holder.prefix).upgrade('parallel')
        finally:
            transaction(self.engine, holder._release)

    def test_replay_checks_actual_schema_and_job_checksum(self):
        migrator = self.migration('replay')
        migrator.upgrade('same-job')
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            migrator.runtime.drop(connection)
        with self.assertRaises(ydb_dbapi.DatabaseError):
            migrator.upgrade('same-job')
        ensure_table(self.engine, migrator.runtime)
        transaction(self.engine, lambda c: c.execute(migrator.jobs.update().values(checksum='0' * 64)))
        with self.assertRaises(ValueError):
            migrator.upgrade('same-job')

    def test_schema_verification_distinguishes_int32_from_int64(self):
        from ydb_sqlalchemy.sqlalchemy.types import Int32
        migrator = self.migration('width')
        wrong = sa.Table(migrator.runtime.name, sa.MetaData(),
                         sa.Column('key', sa.Text, primary_key=True, nullable=False),
                         sa.Column('value', Int32))
        ensure_table(self.engine, wrong)
        expected = sa.Table(wrong.name, sa.MetaData(),
                            sa.Column('key', sa.Text, primary_key=True, nullable=False),
                            sa.Column('value', sa.BigInteger))
        with self.engine.connect() as connection:
            with self.assertRaises(SchemaMismatch):
                verify_table(connection, expected)


if __name__ == '__main__':
    unittest.main()
