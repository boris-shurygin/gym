"""Measured results retain their units, side and historic volume semantics."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from api.analytics import Analytics, exercise_series_key, working_metrics_v2
from api.errors import ServiceError
from contracts.validation import validate

spec = importlib.util.spec_from_file_location('measurement_analytics_helpers', Path(__file__).with_name('test_measurements.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class MeasurementAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.SqliteMeasurementTests(methodName='runTest')
        self.h.setUp()
        self.h.install2()
        self.analytics = Analytics(self.h.auth)

    def tearDown(self):
        self.h.tearDown()

    def completed(self):
        h = self.h
        start = h.start2()
        h.submit(h.add2(start, 'external', side='left', warmup=True))
        for exercise, sides in [('external', ['left', 'right']), ('body', [None]),
                ('duration', ['left', 'right']), ('scale', [None])]:
            for side in sides:
                h.submit(h.add2(start, exercise, side=side))
        for index in range(3):
            h.submit(h.add2(start, 'myo', index=index))
        h.submit(h.add2(start, 'myo', extra=True))
        h.submit(h.op2(start['workout_id'], {'type': 'complete_workout'}, h.current(start)['server_revision']))
        return h.current(start)

    def test_units_counts_and_dumbbell_volume_are_separate(self):
        workout = self.completed()
        metrics = dict(working_metrics_v2(workout))
        myo = next(p for key, p in metrics.items() if key[0] == 'myo')
        self.assertEqual(myo['volume_g_reps'], 680000)
        self.assertEqual((myo['myo_series_count'], myo['myo_component_count']), (1, 4))
        self.assertEqual(myo['ordinary_set_count'], 0)
        body = next(p for key, p in metrics.items() if key[0] == 'body')
        self.assertEqual(body['rep_count'], 10)
        self.assertIsNone(body['max_weight_g'])
        self.assertIsNone(body['volume_g_reps'])
        scale = next(p for key, p in metrics.items() if key[0] == 'scale')
        self.assertEqual(scale['max_scale_milli'], 32500)
        self.assertIsNone(scale['volume_g_reps'])
        duration = [p for key, p in metrics.items() if key[0] == 'duration']
        self.assertEqual(len(duration), 2)
        self.assertEqual([p['duration_seconds'] for p in duration], [45, 45])
        self.assertEqual(sum(p['rep_count'] for p in duration), 0)
        summary = self.analytics.execute('get_summary', self.h.alice, {'schema_version': 2})
        validate('Summary', summary)
        self.assertEqual(summary['working_result_count'], 10)
        self.assertEqual(summary['ordinary_set_count'], 2)
        self.assertEqual(summary['side_result_count'], 4)
        self.assertEqual(summary['myo_series_count'], 1)
        self.assertEqual(summary['duration_seconds'], 90)
        self.assertEqual(summary['rep_count'], 70)
        response = self.h.request('GET', '/api/v1/stats/summary', version=2, query={'schema_version': '2'})
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(response['body'])['analytics_version'], 'stats-v2')
        with self.assertRaises(ServiceError) as caught:
            self.analytics.execute('get_summary', self.h.alice, {})
        self.assertEqual(caught.exception.code, 'upgrade_required')

    def test_series_sides_owner_and_history_correction(self):
        workout = self.completed()
        exercise = next(e for e in workout['plan']['template']['exercises'] if e['exercise_id'] == 'duration')
        query = {'schema_version': 2, 'exercise_id': 'duration', 'equipment_id': exercise['equipment_id'],
            'series_key': exercise_series_key(exercise, 'left')}
        series = self.analytics.execute('get_series', self.h.alice, query)
        self.assertEqual(series['points'][0]['duration_seconds'], 45)
        self.assertEqual(self.analytics.execute('get_series', self.h.bob, query)['points'], [])
        old = next(s for s in workout['sets'] if s['session_exercise_id'] == 'duration' and s['side'] == 'left')
        command = {k: v for k, v in old.items() if k not in ('deleted_at', 'session_exercise_id')}
        command.update(type='edit_set', duration_seconds=38)
        operation = self.h.op2(workout['workout_id'], command, workout['server_revision'])
        self.h.submit(operation)
        changed = self.analytics.execute('get_series', self.h.alice, query)
        self.assertEqual(changed['points'][0]['duration_seconds'], 38)
        right = self.analytics.execute('get_series', self.h.alice, query | {'series_key': exercise_series_key(exercise, 'right')})
        self.assertEqual(right['points'][0]['duration_seconds'], 45)
        self.assertGreater(changed['history_revision'], series['history_revision'])

    def test_legacy_per_implement_volume_is_not_reinterpreted(self):
        workout = self.completed()
        old = copy.deepcopy(workout)
        old['plan'] = copy.deepcopy(self.h.legacy_plan)
        exercise = old['plan']['template']['exercises'][0]
        exercise['weight_convention'] = 'per_implement_kg'
        old['sets'] = [{'session_exercise_id': exercise['template_exercise_id'], 'deleted_at': None,
            'kind': 'working', 'weight_g': 9000, 'reps': 8}]
        key, point = working_metrics_v2(old)[0]
        self.assertEqual(key[2], 'v1|per_implement_kg')
        self.assertEqual(point['volume_g_reps'], 72000)
        self.assertEqual(point['ordinary_set_count'], 1)
        self.assertEqual(point['working_result_count'], 1)

    def test_v2_series_cursor_continues_rest_pages_and_is_owner_bound(self):
        workouts = []
        for seconds in (45, 38):
            start = self.h.start2()
            self.h.submit(self.h.add2(start, 'duration', side='left', duration_seconds=seconds))
            self.h.submit(self.h.op2(start['workout_id'], {'type': 'complete_workout'},
                self.h.current(start)['server_revision']))
            workouts.append(self.h.current(start))
        exercise = next(e for e in workouts[0]['plan']['template']['exercises'] if e['exercise_id'] == 'duration')
        query = {'schema_version': '2', 'exercise_id': 'duration', 'equipment_id': exercise['equipment_id'],
            'series_key': exercise_series_key(exercise, 'left')}
        with patch('api.analytics.PAGE_SIZE', 1):
            response = self.h.request('GET', '/api/v1/stats/exercises/duration', version=2, query=query)
            self.assertEqual(response['statusCode'], 200)
            first = json.loads(response['body'])
            self.assertEqual(len(first['points']), 1)
            self.assertIsNotNone(first['next_cursor'])
            following = query | {'cursor': first['next_cursor']}
            response = self.h.request('GET', '/api/v1/stats/exercises/duration', version=2, query=following)
            self.assertEqual(response['statusCode'], 200)
            second = json.loads(response['body'])
            self.assertEqual(len(second['points']), 1)
            self.assertIsNone(second['next_cursor'])
            points = first['points'] + second['points']
            self.assertEqual({p['workout_id'] for p in points}, {w['workout_id'] for w in workouts})
            self.assertEqual(sorted(p['duration_seconds'] for p in points), [38, 45])
            self.assertEqual(first['history_revision'], second['history_revision'])
            with self.assertRaises(ServiceError) as caught:
                self.analytics.execute('get_series', self.h.bob, following | {'schema_version': 2})
            self.assertEqual(caught.exception.code, 'cursor_expired')
