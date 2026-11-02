"""Совместимость двух моделей и независимые факты измерений/сторон."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from jsonschema import ValidationError
import sqlalchemy as sa

from api.auth import SESSION_COOKIE
from api.errors import ServiceError
from api.handler import handler
from api.store import Store
from api.workouts import Workouts
from contracts.canonical import canonical_bytes, fingerprint
from contracts.validation import validate

spec = importlib.util.spec_from_file_location('measurement_workout_helpers', Path(__file__).with_name('test_workouts.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def target(identifier, weight=None, reps=10, scale=None, seconds=None):
    return {'target_id': identifier, 'weight_g': weight, 'scale_milli': scale,
        'suggested_reps': reps, 'duration_seconds': seconds, 'rep_min': None, 'rep_max': None}


def program2():
    def exercise(identifier, load, targets, *, sides='single', measurement='reps', structure='straight'):
        return {'template_exercise_id': identifier, 'exercise_id': identifier,
            'equipment_id': identifier + '-equipment', 'name': 'Синтетический пример',
            'measurement': measurement, 'load': load, 'sides': sides,
            'set_structure': structure, 'rest_seconds': {'min': 4, 'max': 7} if structure == 'myo' else None,
            'rule': 'fixed', 'targets': targets}
    return {'content_schema_version': 2, 'name': 'Проверка измерений', 'templates': [{
        'template_id': 'measurements', 'name': 'Измерения', 'description': '', 'exercises': [
            exercise('external', {'kind': 'external', 'weight_convention': 'total_external_kg',
                'implement_count': 1, 'step_g': 1000}, [target('external-1', 30000), target('external-2', 35000)], sides='each'),
            exercise('body', {'kind': 'bodyweight'}, [target('body-1'), target('body-2', reps=8)]),
            exercise('duration', {'kind': 'bodyweight'}, [target('duration-1', reps=None, seconds=45)],
                sides='each', measurement='duration'),
            exercise('scale', {'kind': 'machine_scale', 'step_milli': 1000}, [target('scale-1', scale=32500)]),
            exercise('myo', {'kind': 'external', 'weight_convention': 'per_implement_kg',
                'implement_count': 2, 'step_g': 500},
                [target('myo-1', 12000, 15), target('myo-2', 12000, 5), target('myo-3', 10000, 5)], structure='myo')]}]}


class MeasurementCases:
    allow = helpers.WorkoutCases.allow
    begin = helpers.WorkoutCases.begin
    login = helpers.WorkoutCases.login
    execute = helpers.WorkoutCases.execute
    propose = helpers.WorkoutCases.propose
    approve_input = helpers.WorkoutCases.approve_input
    publish = helpers.WorkoutCases.publish
    error = helpers.WorkoutCases.error
    prepare_workouts = helpers.WorkoutCases.prepare_workouts
    work = helpers.WorkoutCases.work
    operation = helpers.WorkoutCases.operation
    submit = helpers.WorkoutCases.submit
    start = helpers.WorkoutCases.start
    add = helpers.WorkoutCases.add
    reject = helpers.WorkoutCases.reject
    current = helpers.WorkoutCases.current

    def prepare_measurements(self):
        self.prepare_workouts()
        self.legacy_plan = copy.deepcopy(self.plan)

    def draft2(self):
        head = self.execute('get_program')['version']
        return self.execute('propose_change', request_id=str(uuid4()), expected_owner_id='alice',
            proposal={'base_program_version_id': head['program_version_id'],
                'content': program2(), 'rationale': 'Проверка новой модели'})

    def install2(self):
        self.publish(self.draft2())
        self.plan = self.work('list_plans')['items'][0]
        self.assertEqual(self.plan['snapshot_schema_version'], 2)
        self.assertEqual(self.plan['calculator_version'], 'fixed-v2')
        self.exercise = self.plan['template']['exercises'][0]

    def op2(self, workout_id=None, command=None, revision=0):
        return self.operation(workout_id, command, revision) | {'schema_version': 2}

    def start2(self):
        op = self.op2()
        self.submit(op)
        return op

    def add2(self, start, exercise_id, *, index=0, side=None, warmup=False, extra=False, **overrides):
        exercise = next(e for e in self.plan['template']['exercises'] if e['template_exercise_id'] == exercise_id)
        goal = exercise['targets'][-1 if extra else index]
        command = {'type': 'add_set', 'session_exercise_id': exercise_id, 'set_id': str(uuid4()),
            'kind': 'warmup' if warmup else 'working', 'side': side,
            'weight_g': goal['weight_g'], 'scale_milli': goal['scale_milli'],
            'reps': goal['suggested_reps'], 'duration_seconds': goal['duration_seconds'],
            'planned_target_id': None if warmup or extra else goal['target_id'],
            'comparison_target_id': None if warmup else goal['target_id']}
        command.update(overrides)
        return self.op2(start['workout_id'], command, self.current(start)['server_revision'])

    def test_v1_outbox_and_receipt_remain_exact_after_v2_publication(self):
        legacy = self.operation()
        original = canonical_bytes(legacy)
        digest = fingerprint('operation', legacy)
        self.install2()
        receipt = self.submit(legacy)
        self.assertEqual(canonical_bytes(legacy), original)
        self.assertEqual(self.current(legacy)['plan'], self.legacy_plan)
        stored = self.store.run(lambda db: db.get('workout_records', 'alice', 'operation:' + legacy['operation_id']))
        self.assertEqual(stored, {'digest': digest, 'receipt': receipt})
        self.assertEqual(self.submit(copy.deepcopy(legacy)), receipt)
        self.assertEqual(self.work('get_plan', plan_snapshot_id=self.legacy_plan['plan_snapshot_id']), self.legacy_plan)
        self.reject('validation_failed', self.operation())
        self.reject('validation_failed', self.op2(command={'type': 'start_workout',
            'plan_snapshot_id': self.legacy_plan['plan_snapshot_id']}))
        modern = self.start2()
        self.reject('validation_failed', self.operation(modern['workout_id'], {'type': 'complete_workout'}, 1))

    def test_sides_keep_independent_slots_and_edit_cannot_move_a_fact(self):
        self.install2()
        start = self.start2()
        left = self.add2(start, 'external', side='left')
        self.submit(left)
        self.assertEqual(len(self.current(start)['sets']), 1)
        self.reject('validation_failed', self.add2(start, 'external', side='left'))
        right = self.add2(start, 'external', side='right')
        self.submit(right)
        edit = {k: v for k, v in left['command'].items() if k != 'session_exercise_id'} | {'type': 'edit_set'}
        self.reject('validation_failed', self.op2(start['workout_id'], edit | {'side': 'right'}, 3))
        self.submit(self.op2(start['workout_id'], edit | {'weight_g': 31000}, 3))
        self.assertEqual(len(self.current(start)['sets']), 2)
        self.submit(self.op2(start['workout_id'], {'type': 'delete_set', 'set_id': left['command']['set_id']}, 4))
        self.submit(self.add2(start, 'external', side='left'))
        results = self.current(start)['sets']
        self.assertIsNotNone(results[0]['deleted_at'])
        self.assertEqual(results[1]['set_id'], right['command']['set_id'])
        self.assertEqual(results[2]['planned_target_id'], 'external-1')
        self.error('not_found', lambda: self.work('get_workout', self.bob, workout_id=start['workout_id']))

    def test_metadata_rejects_fictional_kilograms_reps_duration_and_wrong_sides(self):
        self.install2()
        start = self.start2()
        for exercise, fields in [('body', {'weight_g': 0}), ('body', {'side': 'left'}),
                ('duration', {'side': 'left', 'reps': 45}), ('duration', {'side': None}),
                ('scale', {'weight_g': 32500}), ('scale', {'scale_milli': None}),
                ('external', {'side': 'left', 'weight_g': None})]:
            with self.subTest(exercise=exercise, fields=fields):
                self.reject('validation_failed', self.add2(start, exercise, **fields))
        self.submit(self.add2(start, 'body'))
        self.submit(self.add2(start, 'duration', side='left', duration_seconds=42))
        self.submit(self.add2(start, 'scale'))
        facts = self.current(start)['sets']
        self.assertEqual((facts[0]['weight_g'], facts[0]['reps']), (None, 10))
        self.assertEqual((facts[1]['duration_seconds'], facts[1]['reps']), (42, None))
        self.assertEqual((facts[2]['scale_milli'], facts[2]['weight_g']), (32500, None))

    def test_myo_warmup_and_extra_short_results_preserve_separate_targets(self):
        self.install2()
        start = self.start2()
        self.submit(self.add2(start, 'myo', warmup=True, weight_g=8000))
        self.reject('validation_failed', self.add2(start, 'myo', index=1))
        recorded = []
        for index in range(3):
            op = self.add2(start, 'myo', index=index)
            self.submit(op)
            recorded.append(op)
        extra = self.add2(start, 'myo', extra=True)
        self.submit(extra)
        self.assertEqual(self.submit(copy.deepcopy(extra))['server_revision'], 6)
        facts = self.current(start)['sets']
        self.assertIsNone(facts[0]['comparison_target_id'])
        self.assertEqual([s['reps'] for s in facts[1:]], [15, 5, 5, 5])
        self.assertEqual([s['weight_g'] for s in facts[1:]], [12000, 12000, 10000, 10000])
        self.assertEqual((facts[-1]['planned_target_id'], facts[-1]['comparison_target_id']), (None, 'myo-3'))
        self.submit(self.op2(start['workout_id'], {'type': 'delete_set', 'set_id': recorded[1]['command']['set_id']}, 6))
        self.submit(self.add2(start, 'myo', index=1))
        self.assertIsNone(self.current(start)['sets'][-2]['planned_target_id'])
        self.assertEqual(self.current(start)['sets'][-1]['planned_target_id'], 'myo-2')

    def test_program_semantics_rejects_invalid_units_and_myo_without_short_target(self):
        valid = program2()
        validate('ProgramContent', valid)
        changes = [lambda es: es[0]['load'].update(implement_count=2),
            lambda es: es[1]['targets'][0].update(weight_g=0),
            lambda es: es[2]['targets'][0].update(suggested_reps=30),
            lambda es: es[3]['targets'][0].update(scale_milli=None),
            lambda es: es[4].update(targets=es[4]['targets'][:1]),
            lambda es: es[4].update(rest_seconds={'min': 7, 'max': 4})]
        for change in changes:
            content = copy.deepcopy(valid)
            change(content['templates'][0]['exercises'])
            with self.assertRaises((ValueError, ValidationError)):
                validate('ProgramContent', content)

    def request(self, method, path, body=None, version=None, query=None):
        headers = {'cookie': SESSION_COOKIE + '=' + self.alice, 'origin': self.auth.settings.origin,
            'x-csrf-token': self.auth.session(self.alice)['csrf_token'], 'content-type': 'application/json'}
        if version is not None:
            headers['x-gym-schema-version'] = str(version)
        with patch('api.handler.application', return_value=self.auth):
            return handler({'httpMethod': method, 'path': path, 'headers': headers,
                'body': canonical_bytes(body).decode() if body is not None else '',
                'queryStringParameters': query or {}})

    def test_rest_requires_opt_in_without_blocking_old_outbox(self):
        legacy = self.operation()
        self.install2()
        for path in ('/api/v1/program', '/api/v1/plans', '/api/v1/exercises', '/api/v1/sync/bootstrap'):
            old = self.request('GET', path)
            self.assertEqual(json.loads(old['body'])['error']['code'], 'upgrade_required', path)
            self.assertEqual(self.request('GET', path, version=2)['statusCode'], 200, path)
        self.assertEqual(self.request('POST', '/api/v1/sync/operations', legacy)['statusCode'], 200)
        modern = self.op2()
        self.assertEqual(self.request('POST', '/api/v1/sync/operations', modern)['statusCode'], 409)
        self.assertEqual(self.request('POST', '/api/v1/sync/operations', modern, version=2)['statusCode'], 200)
        self.assertEqual(self.request('GET', '/api/v1/workouts')['statusCode'], 409)
        mixed = self.request('GET', '/api/v1/workouts', version=2)
        self.assertEqual({w['plan']['snapshot_schema_version'] for w in json.loads(mixed['body'])['items']}, {1, 2})

    def test_old_client_cannot_publish_v2_then_receive_an_upgrade_error(self):
        old_head = self.execute('get_program')['version']['program_version_id']
        change = self.draft2()
        self.execute('validate_change', change_id=change['change_id'], expected_revision=1)
        body = self.approve_input(change)
        route_body = {key: value for key, value in body.items() if key != 'change_id'}
        path = '/api/v1/program/changes/' + change['change_id'] + '/approve-and-publish'
        old = self.request('POST', path, route_body)
        self.assertEqual(json.loads(old['body'])['error']['code'], 'upgrade_required')
        self.assertEqual(self.execute('get_program')['version']['program_version_id'], old_head)
        self.assertIsNone(self.store.run(lambda db: db.get('program_records', 'alice', 'publication:' + change['change_id'])))
        self.assertEqual(self.request('POST', path, route_body, version=2)['statusCode'], 200)


class SqliteMeasurementTests(MeasurementCases, unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.prefix = 'gym_measurements_'
        for table in Store(self.engine, self.prefix).tables.values():
            table.create(self.engine)
        self.prepare_measurements()

    def tearDown(self):
        self.engine.dispose()
