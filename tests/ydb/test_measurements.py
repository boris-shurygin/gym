"""Выполни инварианты schema 2 на одноразовой YDB без промышленных данных."""

import importlib.util
from pathlib import Path
import unittest
from uuid import uuid4

from api.database import DatabaseSettings, create_engine
from api.store import Store
from db.schema import ensure_table

spec = importlib.util.spec_from_file_location('measurement_cases', Path(__file__).parents[1] / 'api/test_measurements.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


class YdbMeasurementTests(cases.MeasurementCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = DatabaseSettings.from_env()
        if settings.auth != 'anonymous':
            raise RuntimeError('Используй только одноразовую local-ydb в CI')
        cls.engine = create_engine(settings)
        cls.prefix = 'gym_measurements_' + uuid4().hex[:10] + '_'
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
        self.prepare_measurements()
