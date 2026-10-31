"""P09 на настоящей одноразовой YDB, включая конфликт расчёта и исправления."""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path
import unittest
from uuid import uuid4
from unittest.mock import patch

from api.analytics import Analytics
from api.database import DatabaseSettings, create_engine
from api.store import Store
from db.schema import ensure_table

spec = importlib.util.spec_from_file_location('progression_cases', Path(__file__).parents[1] / 'api/test_progression.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


class YdbProgressionTests(cases.ProgressionCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = DatabaseSettings.from_env()
        if settings.auth != 'anonymous':
            raise RuntimeError('Используй только одноразовую local-ydb в CI')
        cls.engine = create_engine(settings)
        cls.prefix = 'gym_p09_' + uuid4().hex[:10] + '_'
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
        self.prepare_progression()

    def test_concurrent_correction_cannot_publish_calculation_h_as_fresh_h_plus_one(self):
        start, added, _ = self.complete()
        entered, resume = Event(), Event()
        calculate = Analytics._calculate
        revisions = []

        def blocked(db, owner, operation, criteria, zone, begin, end, revision, now):
            revisions.append(revision)
            if len(revisions) == 1:
                entered.set()
                if not resume.wait(20):
                    raise RuntimeError('Гонка не была запущена')
            return calculate(db, owner, operation, criteria, zone, begin, end, revision, now)

        with patch.object(Analytics, '_calculate', staticmethod(blocked)), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.series)
            try:
                self.assertTrue(entered.wait(20))
                command = {k: v for k, v in added['command'].items() if k != 'session_exercise_id'}
                self.h.submit(self.h.operation(start['workout_id'], command | {
                    'type': 'edit_set', 'weight_g': 60001, 'reps': 8}, revision=3))
            finally:
                resume.set()
            report = future.result(timeout=30)
        self.assertGreater(len(revisions), 1)
        self.assertEqual(report['freshness'], 'fresh')
        self.assertEqual(report['history_revision'], revisions[-1])
        self.assertEqual(report['points'][0]['volume_g_reps'], 480008)
