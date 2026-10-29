"""Те же прикладные инварианты на SERIALIZABLE YDB, включая гонку публикаций."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import unittest
from uuid import uuid4

import sqlalchemy as sa

from api.database import DatabaseSettings, create_engine
from api.errors import ServiceError
from api.store import Store
from db.schema import ensure_table

spec = importlib.util.spec_from_file_location('application_cases', Path(__file__).parents[1] / 'api/test_accounts_programs.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


class YdbApplicationTests(cases.ApplicationCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = DatabaseSettings.from_env()
        if settings.auth != 'anonymous':
            raise RuntimeError('Эти тесты работают только с одноразовой local-ydb в CI')
        cls.engine = create_engine(settings)
        cls.prefix = 'gym_p06_' + uuid4().hex[:10] + '_'
        for table in Store(cls.engine, cls.prefix).tables.values():
            ensure_table(cls.engine, table)

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
                for table in Store(cls.engine, cls.prefix).tables.values():
                    table.drop(connection)
        finally:
            cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            for table in Store(self.engine, self.prefix).tables.values():
                connection.execute(table.delete())
        self.prepare()

    def test_concurrent_publication_has_one_winner(self):
        changes = [self.propose(weight=w) for w in (60000, 65000)]
        for change in changes:
            self.execute('validate_change', change_id=change['change_id'], expected_revision=1)
        inputs = [self.approve_input(change) for change in changes]
        def publish(value):
            try:
                return self.programs.execute('approve_and_publish', self.alice, value)
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(publish, inputs))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn('program_conflict', results)
        self.assertEqual(self.execute('get_program')['version']['version'], 1)

    def test_concurrent_callback_consumes_state_once(self):
        state, binder = self.begin()
        self.provider.identity.return_value = '101'
        self.provider.identity.reset_mock()
        def finish(_):
            try:
                return self.auth.callback({'state': state, 'code': 'one-use-code'}, binder, 'concurrent')
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(finish, range(2)))
        self.assertEqual(sum(isinstance(r, tuple) for r in results), 1)
        self.assertIn('invalid_request', results)
        self.assertEqual(self.provider.identity.call_count, 1)
