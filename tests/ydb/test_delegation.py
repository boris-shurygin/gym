"""Проверки делегирования и конкурентной ротации на одноразовой YDB."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import unittest
from uuid import uuid4

from api.database import DatabaseSettings, create_engine
from api.delegation import OAuthError
from api.store import Store
from db.schema import ensure_table

spec = importlib.util.spec_from_file_location('delegation_cases', Path(__file__).parents[1] / 'api/test_delegation.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


class YdbDelegationTests(cases.DelegationCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = DatabaseSettings.from_env()
        if settings.auth != 'anonymous':
            raise RuntimeError('Только одноразовая local-ydb без облачных credentials')
        cls.engine, cls.prefix = create_engine(settings), 'gym_p10_' + uuid4().hex[:10] + '_'
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
        self.prepare_delegation()

    def test_concurrent_refresh_has_one_exchange_and_revokes_family(self):
        first, _ = self.issue()
        def exchange(_):
            try:
                return self.refresh(first['refresh_token'])
            except OAuthError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(exchange, range(2)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn('invalid_grant', results)
        winner = next(result for result in results if isinstance(result, dict))
        self.assertEqual(self.rpc(winner['access_token'])['statusCode'], 401)
