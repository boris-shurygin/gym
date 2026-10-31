"""P09: фиксированные назначения, исправления и воспроизводимые производные факты."""

import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
from jsonschema import ValidationError

from api.analytics import Analytics, working_metrics
from api.auth import SESSION_COOKIE
from api.errors import ServiceError
from api.handler import handler
from api.store import Store
from api.sync_state import metadata
from api.workouts import date_key, revision_key
from contracts.validation import validate

spec = importlib.util.spec_from_file_location('progression_workouts', Path(__file__).with_name('test_workouts.py'))
workout_cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workout_cases)


class ProgressionCases:
    def prepare_progression(self):
        self.h = workout_cases.SqliteWorkoutTests(methodName='runTest')
        self.h.engine, self.h.prefix = self.engine, self.prefix
        self.h.prepare_workouts()
        self.analytics = Analytics(self.h.auth)

    def summary(self, **value):
        return self.analytics.execute('get_summary', self.h.alice, value)

    def series(self, token=None, **value):
        return self.analytics.execute('get_series', token or self.h.alice,
            {k: self.h.exercise[k] for k in ('exercise_id', 'equipment_id', 'weight_convention')} | value)

    def complete(self, warmup=False):
        start, _ = self.h.start()
        added = self.h.add(start, warmup=warmup)
        self.h.submit(added)
        finished = self.h.operation(start['workout_id'], {'type': 'complete_workout'}, revision=2)
        self.h.submit(finished)
        return start, added, finished

    def seed(self, started_at, *, status='completed', sets=None, plan=None):
        """Только синтетические данные календаря/страниц в одноразовой БД теста."""
        workout = {'workout_id': str(uuid4()), 'owner_id': 'alice', 'server_revision': 1,
            'plan': copy.deepcopy(plan or self.h.plan), 'started_at': started_at,
            'completed_at': started_at if status == 'completed' else None,
            'updated_at': started_at, 'status': status, 'sets': sets if sets is not None else [{
                'set_id': str(uuid4()), 'session_exercise_id': self.h.exercise['template_exercise_id'],
                'kind': 'working', 'weight_g': 62501, 'reps': 8, 'deleted_at': None,
                'planned_target_id': self.h.exercise['targets'][0]['target_id'],
                'comparison_target_id': self.h.exercise['targets'][0]['target_id']}]}
        validate('Workout', workout)
        def save(db):
            owner, identifier = workout['owner_id'], workout['workout_id']
            db.put('workout_records', owner, 'workout:' + identifier, workout)
            db.put('workout_records', owner, revision_key(identifier, 1), workout)
            db.put('workout_records', owner, 'history:' + date_key(started_at) + ':' + identifier,
                {'workout_id': identifier, 'revision': 1, 'status': status})
            state = metadata(db, owner)
            state['history_revision'] += 1
            db.put('workout_records', owner, 'meta', state)
        self.h.store.run(save)
        return workout

    def test_correction_recalculates_reports_without_rewriting_plans_or_audit(self):
        start, added, finished = self.complete()
        before = self.series()
        old_snapshot = copy.deepcopy(self.h.current(start)['plan'])
        self.assertEqual(before['points'][0]['volume_g_reps'], 630000)
        command = {k: v for k, v in added['command'].items() if k != 'session_exercise_id'}
        edit = self.h.operation(start['workout_id'], command | {
            'type': 'edit_set', 'weight_g': 60001, 'reps': 8}, revision=3)
        receipt = self.h.submit(edit)
        corrected = self.series()
        self.assertEqual(corrected['points'][0]['volume_g_reps'], 480008)
        self.assertGreater(corrected['history_revision'], before['history_revision'])
        self.assertEqual(self.summary()['rep_count'], 8)
        self.assertEqual(self.h.submit(edit), receipt)
        self.assertEqual(self.series(), corrected)
        self.assertEqual(self.h.current(start)['plan'], old_snapshot)
        self.assertEqual(self.h.work('get_plan', plan_snapshot_id=old_snapshot['plan_snapshot_id']), old_snapshot)
        self.assertEqual(self.h.store.run(lambda db: db.get('workout_records', 'alice',
            revision_key(start['workout_id'], 3)))['sets'][0]['weight_g'], 70000)
        fresh_plan = self.h.work('list_plans')['items'][0]
        self.assertEqual(fresh_plan['template'], old_snapshot['template'])
        self.assertEqual(fresh_plan['history_revision'], corrected['history_revision'])
        deleted = self.h.operation(start['workout_id'], {'type': 'delete_set',
            'set_id': added['command']['set_id']}, revision=4)
        self.h.submit(deleted)
        self.assertEqual(self.summary()['workout_count'], 0)
        self.assertEqual(self.series()['points'], [])
        self.assertEqual(before['points'][0]['volume_g_reps'], 630000)

    def test_fixed_mixed_targets_exact_grams_and_no_automatic_deload(self):
        old = copy.deepcopy(self.h.plan)
        proposal = self.h.propose(base=old['program_version_id'], weight=62501)
        targets = proposal['proposal']['content']['templates'][0]['exercises'][0]['targets']
        targets.append(targets[0] | {'target_id': 'mixed-second', 'weight_g': 67503})
        proposal = self.h.execute('replace_change', request_id=str(uuid4()), expected_owner_id='alice',
            change_id=proposal['change_id'], expected_revision=1, proposal=proposal['proposal'])
        self.assertEqual(self.h.work('list_plans')['items'][0]['template'], old['template'])
        published = self.h.publish(proposal)
        plan = self.h.work('list_plans')['items'][0]
        self.assertEqual(plan['template']['exercises'][0]['targets'][0]['weight_g'], 62501)
        self.assertEqual(plan['template']['exercises'][0]['targets'][1]['weight_g'], 67503)
        self.assertEqual(self.h.work('get_plan', plan_snapshot_id=old['plan_snapshot_id']), old)
        self.complete()  # Подлинный старый офлайн-план после новой публикации.
        current = self.h.work('list_plans')['items'][0]
        self.assertEqual(current['template'], plan['template'])
        self.assertEqual(current['program_version_id'], published['program_version']['program_version_id'])
        self.h.clock += 40 * 86400
        self.h.alice = self.h.login('101', 'alice')
        self.assertEqual(self.h.work('list_plans')['items'][0]['template'], plan['template'])

    def test_filters_ties_zero_weight_and_equipment_are_explicit(self):
        workout = self.seed('2026-10-29T12:00:00Z')
        first = workout['sets'][0]
        results = [first | {'weight_g': 0, 'reps': 10},
            first | {'set_id': str(uuid4()), 'weight_g': 0, 'reps': 12, 'planned_target_id': None},
            first | {'set_id': str(uuid4()), 'kind': 'warmup', 'weight_g': 200000, 'reps': 9,
                'planned_target_id': None, 'comparison_target_id': None},
            first | {'set_id': str(uuid4()), 'weight_g': 300000, 'deleted_at': workout['updated_at']}]
        self.seed('2026-10-29T13:00:00Z', sets=results)
        self.seed('2026-10-29T14:00:00Z', status='active')
        self.seed('2026-10-29T15:00:00Z', status='cancelled', sets=[])
        self.seed('2026-10-29T16:00:00Z', sets=[results[2]])
        alternate = copy.deepcopy(self.h.plan)
        alternate['template']['exercises'][0]['equipment_id'] = 'another_machine'
        self.seed('2026-10-29T17:00:00Z', plan=alternate)
        report = self.summary()
        self.assertEqual((report['workout_count'], report['working_set_count'], report['rep_count']), (3, 4, 38))
        points = self.series()['points']
        self.assertEqual(len(points), 2)
        self.assertEqual((points[1]['max_weight_g'], points[1]['max_weight_reps'], points[1]['volume_g_reps']), (0, 12, 0))
        self.assertEqual(self.series(equipment_id='another_machine')['points'][0]['volume_g_reps'], 500008)
        self.assertEqual(self.series(weight_convention='per_implement_kg')['points'], [])
        self.assertEqual(working_metrics(workout | {'status': 'active'}), [])

    def test_rebuild_from_history_is_repeatable_and_never_uses_partial_local_data(self):
        self.complete()
        before, series = self.summary(), self.series()
        def evict(db):
            for key, _ in db.scan('workout_records', 'alice', 'stats-cache:'):
                db.delete('workout_records', 'alice', key)
        self.h.store.run(evict)
        self.assertEqual(self.summary(), before)
        self.assertEqual(self.series(), series)
        self.assertTrue(before['complete'])
        self.assertEqual(self.analytics.execute('get_summary', self.h.bob, {})['workout_count'], 0)

    def test_weight_tie_selects_reps_and_unsupported_rule_is_rejected(self):
        workout = self.seed('2026-10-29T12:00:00Z')
        first = workout['sets'][0]
        metrics = working_metrics(workout | {'sets': [first | {'reps': 8},
            first | {'set_id': str(uuid4()), 'reps': 12}]})[0][1]
        self.assertEqual((metrics['max_weight_g'], metrics['max_weight_reps']), (62501, 12))
        change = self.h.propose(base=self.h.plan['program_version_id'])
        change['proposal']['content']['templates'][0]['exercises'][0]['rule'] = 'double_progression'
        with self.assertRaises(ValidationError):
            self.h.execute('replace_change', request_id=str(uuid4()), expected_owner_id='alice',
                change_id=change['change_id'], expected_revision=1, proposal=change['proposal'])

    def test_oversized_report_fails_without_marking_partial_data_complete(self):
        self.complete()
        with patch('api.analytics.MAX_MANIFEST_BYTES', 100), self.assertRaises(ServiceError) as error:
            self.series()
        self.assertEqual(error.exception.code, 'unavailable')
        self.assertEqual(len(self.series()['points']), 1)

    def test_calendar_boundaries_follow_profile_timezone_and_dst(self):
        self.h.clock = int(datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp())
        self.h.alice = self.h.login('101', 'alice')
        self.h.execute('set_profile', request_id=str(uuid4()), expected_owner_id='alice', analytics_timezone='Europe/Berlin')
        for value in ['2026-10-18T21:59:59.999999Z', '2026-10-18T22:00:00Z',
                '2026-10-25T22:59:59Z', '2026-10-25T23:00:00Z']:
            self.seed(value)
        report = self.summary(period_days=7)
        self.assertEqual((report['start_date'], report['end_date_exclusive']), ('2026-10-19', '2026-10-26'))
        self.assertEqual(report['workout_count'], 2)
        self.assertEqual([row['date'] for row in report['activity']], ['2026-10-19', '2026-10-25'])
        self.assertEqual(self.summary(period_days=7, timezone='UTC')['workout_count'], 2)
        self.assertNotEqual(self.summary(period_days=7, timezone='UTC')['activity'], report['activity'])
        for name in ('Unknown/Timezone', 'Europe'):
            with self.assertRaises(ServiceError) as error:
                self.summary(timezone=name)
            self.assertEqual(error.exception.code, 'validation_failed')

    def test_paged_series_is_owner_bound_and_never_calls_old_revision_fresh(self):
        for _ in range(101):
            self.seed('2026-10-29T12:00:00Z')
        first = self.series()
        self.assertTrue(first['complete'])
        self.assertEqual(len(first['points']), 100)
        cursor = first['next_cursor']
        self.assertIsNotNone(cursor)
        self.seed('2026-10-29T13:00:00Z')
        second = self.series(cursor=cursor)
        self.assertEqual(second['freshness'], 'stale')
        self.assertEqual(second['history_revision'], first['history_revision'])
        self.assertEqual(len(second['points']), 1)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(self.series()['freshness'], 'fresh')
        for token, values in [(self.h.bob, {}), (self.h.alice, {'equipment_id': 'other'}),
                (self.h.alice, {'period_days': 7})]:
            with self.assertRaises(ServiceError) as error:
                self.series(token, cursor=cursor, **values)
            self.assertEqual(error.exception.code, 'cursor_expired')
        self.h.clock += 901
        with self.assertRaises(ServiceError) as error:
            self.series(cursor=cursor)
        self.assertEqual(error.exception.code, 'cursor_expired')

    def test_http_read_authorization_and_integer_query_validation(self):
        self.complete()
        def request(query=None, token=None):
            with patch('api.handler.application', return_value=self.h.auth):
                return handler({'httpMethod': 'GET', 'path': '/api/v1/stats/summary',
                    'headers': {'cookie': SESSION_COOKIE + '=' + (token or self.h.alice)},
                    'queryStringParameters': query or {}})
        response = request({'period_days': '7', 'timezone': 'UTC'})
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(response['body'])['working_set_count'], 1)
        for value in ['7.0', '07', '91']:
            self.assertEqual(request({'period_days': value})['statusCode'], 400)
        self.assertEqual(request({'owner_id': 'alice'})['statusCode'], 400)
        self.assertEqual(json.loads(request(token=self.h.bob)['body'])['workout_count'], 0)
        self.assertEqual(request(token='invalid')['statusCode'], 401)


class SqliteProgressionTests(ProgressionCases, unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.prefix = 'gym_progression_'
        for table in Store(self.engine, self.prefix).tables.values():
            table.create(self.engine)
        self.prepare_progression()

    def tearDown(self):
        self.engine.dispose()
