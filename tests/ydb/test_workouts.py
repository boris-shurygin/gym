"""Те же P07-сценарии и гонки на настоящей одноразовой YDB в CI."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import unittest
from uuid import uuid4

from api.database import DatabaseSettings, create_engine
from api.errors import ServiceError
from api.store import Store
from db.schema import ensure_table

spec = importlib.util.spec_from_file_location('workout_cases', Path(__file__).parents[1] / 'api/test_workouts.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


class YdbWorkoutTests(cases.WorkoutCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = DatabaseSettings.from_env()
        if settings.auth != 'anonymous':
            raise RuntimeError('Используй только одноразовую local-ydb в CI')
        cls.engine = create_engine(settings)
        cls.prefix = 'gym_p07_' + uuid4().hex[:10] + '_'
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
        self.prepare_workouts()

    def parallel(self, operations):
        def run(operation):
            try:
                return self.submit(operation)
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(run, operations))

    def test_two_simultaneous_retries_have_one_effect_and_same_receipt(self):
        start = self.operation()
        first, second = self.parallel([start, start])
        self.assertEqual(first, second)
        self.assertEqual(self.current(start)['server_revision'], 1)

    def test_two_simultaneous_edits_have_one_winner_and_durable_conflict(self):
        start, _ = self.start()
        left, right = self.add(start), self.add(start, warmup=True)
        results = self.parallel([left, right])
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn('revision_conflict', results)
        self.assertEqual(len(self.current(start)['sets']), 1)

    def test_independent_devices_can_start_distinct_active_workouts(self):
        operations = [self.operation(), self.operation()]
        results = self.parallel(operations)
        self.assertTrue(all(isinstance(r, dict) for r in results))
        self.assertEqual(len(self.work('list_workouts', status='active')['items']), 2)
