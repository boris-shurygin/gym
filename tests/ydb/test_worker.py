from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from api.database import DatabaseSettings, create_engine, transaction
from db.migrate import Migrator, MigrationBusy
from db.worker import Worker, RepairRequired


class WorkerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = DatabaseSettings.from_env()
        if settings.auth != 'anonymous' and (os.environ.get('GITHUB_ACTIONS')
                or os.environ.get('GYM_YDB_TEST_TARGET') != 'staging'
                or settings.database != '/ru-central1/b1ghmudqihbvpnscd2fq/etn63tldguvkj5kcpi3r'):
            raise RuntimeError('Облачный тест допустим только оператору в staging')
        cls.engine = create_engine(settings, token=os.environ.get('GYM_YDB_TOKEN'))
        cls.prefix = 'gym_worker_test_' + uuid4().hex[:12] + '_'
        if settings.auth != 'anonymous':
            last = [0.0]
            @sa.event.listens_for(cls.engine, 'before_execute')
            def pace(connection, statement, multiparams, params, execution_options):
                if isinstance(statement, sa.schema.DDLElement):
                    time.sleep(max(0, 1.5 - (time.monotonic() - last[0])))
                    last[0] = time.monotonic()

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
                for name in sa.inspect(connection).get_table_names():
                    if name.startswith(cls.prefix):
                        sa.Table(name, sa.MetaData()).drop(connection)
        finally:
            cls.engine.dispose()

    def worker(self, suffix):
        worker = Worker(self.engine, 'validation', self.prefix + suffix + '_')
        worker.bootstrap()
        return worker

    def ticket(self, row):
        return {'job_id': row['job_id'], 'owner': row['owner'], 'generation': row['generation'],
                'not_before': int(time.time()) - 1, 'evidence_sha256': '1' * 64}

    def test_two_steps_replay_and_release_binding(self):
        worker = self.worker('steps')
        self.assertEqual(worker.status('j')['state'], 'not_found')
        first = worker.step('j')
        self.assertEqual((first['state'], first['checkpoint']), ('needs_resume', 1))
        self.assertEqual(worker.step('j')['state'], 'succeeded')
        self.assertEqual(worker.step('j')['state'], 'succeeded')
        other = Worker(self.engine, 'other-release', worker.migrator.prefix)
        with self.assertRaises(ValueError):
            other.step('j')
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            worker.migrator.runtime.drop(connection)
        with self.assertRaises(Exception):
            worker.status('j')  # cached succeeded не скрывает потерю схемы.

    def test_crash_lock_recovery_compare_and_set_and_fencing(self):
        worker = self.worker('recover')
        def crash():
            raise SystemExit('Process stopped')
        with self.assertRaises(SystemExit):
            worker.step('crashed', after_acquire=crash)
        row = worker.status('crashed')
        self.assertEqual(row['state'], 'running')
        with self.assertRaises(MigrationBusy):
            worker.step('other')
        ticket = self.ticket(row)
        with self.assertRaises(MigrationBusy):
            worker.recover(ticket | {'owner': '0' * 32})
        with self.assertRaises(RepairRequired):
            worker.recover(ticket | {'not_before': int(time.time()) + 600})
        result = worker.recover(ticket)
        self.assertEqual(result['state'], 'needs_resume')
        self.assertEqual(worker.recover(ticket)['recovery_id'], result['recovery_id'])
        stale = Migrator(self.engine, worker.migrator.prefix)
        stale.owner = row['owner']
        with self.assertRaises(MigrationBusy):
            stale._record('crashed', 'failed', worker.digest)
        self.assertEqual(worker.step('crashed')['generation'], 2)
        self.assertEqual(worker.step('crashed')['state'], 'succeeded')
        with self.assertRaises(MigrationBusy):
            worker.recover(ticket)

    def test_partial_step_retains_lock_and_resumes_after_confirmation(self):
        worker = self.worker('partial')
        original = Migrator.upgrade_locked
        def lost_response(migrator, job_id, digest):
            original(migrator, job_id, digest)
            raise RuntimeError('Lost response after DDL')
        with patch.object(Migrator, 'upgrade_locked', lost_response), self.assertRaises(RuntimeError):
            worker.step('partial')
        row = worker.status('partial')
        self.assertEqual(row['state'], 'repair_required')
        with self.assertRaises(RepairRequired):
            worker.step('partial')
        worker.recover(self.ticket(row))
        self.assertEqual(worker.step('partial')['state'], 'needs_resume')
        self.assertEqual(worker.step('partial')['state'], 'succeeded')

    def test_concurrent_requests_have_one_lease_owner(self):
        worker = self.worker('concurrent')
        acquired, release = threading.Event(), threading.Event()
        def pause():
            acquired.set()
            if not release.wait(10):
                raise RuntimeError('Test timeout')
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(worker.step, 'one', after_acquire=pause)
            try:
                self.assertTrue(acquired.wait(10))
                with self.assertRaises(MigrationBusy):
                    worker.step('two')
            finally:
                release.set()
            self.assertEqual(task.result(timeout=20)['state'], 'needs_resume')


if __name__ == '__main__':
    unittest.main()
