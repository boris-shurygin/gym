"""Общие прикладные сценарии P07 для SQLite и настоящей YDB в CI."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from api.auth import SESSION_COOKIE
from api.errors import ServiceError
from api.handler import handler
from api.store import Documents, Store
from api.workouts import Workouts, revision_key
from contracts.canonical import canonical_bytes
from contracts.validation import validate

spec = importlib.util.spec_from_file_location('account_helpers', Path(__file__).with_name('test_accounts_programs.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class WorkoutCases:
    allow = helpers.ApplicationCases.allow
    begin = helpers.ApplicationCases.begin
    login = helpers.ApplicationCases.login
    execute = helpers.ApplicationCases.execute
    propose = helpers.ApplicationCases.propose
    approve_input = helpers.ApplicationCases.approve_input
    publish = helpers.ApplicationCases.publish
    error = helpers.ApplicationCases.error

    def prepare_workouts(self):
        helpers.ApplicationCases.prepare(self)
        self.workouts = Workouts(self.auth)
        self.publish(self.propose())
        self.plan = self.work('list_plans')['items'][0]
        self.exercise = self.plan['template']['exercises'][0]

    def work(self, operation, token=None, **value):
        return self.workouts.execute(operation, token or self.alice, value)

    def operation(self, workout_id=None, command=None, revision=0, after=None):
        return {'schema_version': 1, 'operation_id': str(uuid4()), 'expected_owner_id': 'alice',
            'device_id': str(uuid4()), 'workout_id': workout_id or str(uuid4()),
            'precondition': {'after_operation_id': after} if after else {'server_revision': revision},
            'client_occurred_at': '2026-10-29T15:00:00+03:00',
            'command': command or {'type': 'start_workout', 'plan_snapshot_id': self.plan['plan_snapshot_id']}}

    def submit(self, operation, token=None):
        return self.workouts.execute('submit_operation', token or self.alice, operation)

    def start(self):
        operation = self.operation()
        return operation, self.submit(operation)

    def add(self, start, *, revision=1, after=None, warmup=False, target=None):
        target = target or self.exercise['targets'][0]['target_id']
        return self.operation(start['workout_id'], {'type': 'add_set',
            'session_exercise_id': self.exercise['template_exercise_id'], 'set_id': str(uuid4()),
            'kind': 'warmup' if warmup else 'working', 'weight_g': 70000, 'reps': 9,
            'planned_target_id': None if warmup else target,
            'comparison_target_id': None if warmup else target}, revision, after)

    def reject(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            self.submit(operation)
        self.assertEqual(caught.exception.code, code)
        return caught.exception.receipt

    def current(self, operation):
        return self.work('get_workout', workout_id=operation['workout_id'])

    def test_plans_are_server_owned_fixed_immutable_and_old_starts_survive_publication(self):
        first = copy.deepcopy(self.plan)
        self.assertEqual(self.work('list_plans')['items'][0], first)
        newer = self.publish(self.propose(base=first['program_version_id'], weight=65000))
        latest = self.work('list_plans')['items'][0]
        self.assertNotEqual(latest['plan_snapshot_id'], first['plan_snapshot_id'])
        self.assertEqual(latest['program_version_id'], newer['program_version']['program_version_id'])
        self.assertEqual(self.work('get_plan', plan_snapshot_id=first['plan_snapshot_id']), first)
        start, _ = self.start()
        self.assertEqual(self.current(start)['plan'], first)
        self.submit(self.add(start))
        refreshed = self.work('list_plans')
        self.assertEqual(refreshed['history_revision'], 2)
        self.assertEqual(refreshed['freshness'], 'fresh')
        self.assertEqual(self.current(start)['plan'], first)

    def test_same_operation_survives_lost_response_and_later_revisions(self):
        start, receipt = self.start()
        validate('AppliedReceipt', receipt)
        self.submit(self.add(start, after=start['operation_id']))
        self.assertEqual(self.submit(copy.deepcopy(start)), receipt)
        self.assertEqual(self.work('get_receipt', operation_id=start['operation_id']), receipt)
        for key, value in [('device_id', str(uuid4())), ('client_occurred_at', '2026-10-29T13:00:00Z')]:
            self.reject('operation_id_reused', start | {key: value})
        self.assertEqual(self.current(start)['server_revision'], 2)

    def test_conflict_is_final_and_resolution_requires_new_id(self):
        start, _ = self.start()
        first, conflicting = self.add(start), self.add(start)
        self.submit(first)
        receipt = self.reject('revision_conflict', conflicting)
        self.assertEqual(receipt['observed_server_revision'], 2)
        self.assertEqual(self.work('get_receipt', operation_id=conflicting['operation_id']), receipt)
        warmup = self.add(start, revision=2, warmup=True)
        self.submit(warmup)
        self.assertEqual(self.reject('revision_conflict', conflicting), receipt)
        corrected = conflicting | {'operation_id': str(uuid4()), 'precondition': {'server_revision': 3},
            'command': self.add(start, warmup=True)['command']}
        self.assertEqual(self.submit(corrected)['server_revision'], 4)
        audit = self.store.run(lambda db: db.get('workout_records', 'alice',
            'rejection:' + start['workout_id'] + ':' + conflicting['operation_id']))
        self.assertEqual(audit['operation'], conflicting)

    def test_pending_dependency_receipt_and_changed_body(self):
        start = self.operation()
        add = self.add(start, after=start['operation_id'])
        self.assertIsNone(self.reject('dependency_pending', add))
        self.error('not_found', lambda: self.work('get_receipt', operation_id=add['operation_id']))
        self.reject('operation_id_reused', add | {'device_id': str(uuid4())})
        self.submit(start)
        result = self.submit(add)
        self.assertEqual(result['server_revision'], 2)
        self.assertEqual(self.submit(add), result)

    def test_dependency_cannot_follow_another_workout_rejection_or_cycle(self):
        first, _ = self.start()
        second, _ = self.start()
        invalid = self.add(first, after=second['operation_id'])
        self.reject('invalid_dependency', invalid)
        descendant = self.add(first, after=invalid['operation_id'])
        self.reject('dependency_failed', descendant)
        left, right = self.add(first), self.add(first)
        left['precondition'] = {'after_operation_id': right['operation_id']}
        right['precondition'] = {'after_operation_id': left['operation_id']}
        self.reject('dependency_pending', left)
        self.reject('invalid_dependency', right)
        self.reject('dependency_failed', left)
        self.assertEqual(self.current(first)['server_revision'], 1)

    def test_target_occupancy_extra_sets_tombstones_and_binding_preserved(self):
        start, _ = self.start()
        recorded = []
        revision = 1
        for target in self.exercise['targets']:
            add = self.add(start, revision=revision, target=target['target_id'])
            self.submit(add)
            recorded.append(add)
            revision += 1
        extra = self.add(start, revision=revision)
        extra['command'].update(planned_target_id=None, comparison_target_id=self.exercise['targets'][-1]['target_id'])
        self.submit(extra)
        revision += 1
        delete = self.operation(start['workout_id'], {'type': 'delete_set', 'set_id': recorded[0]['command']['set_id']}, revision)
        self.submit(delete)
        revision += 1
        current = self.current(start)
        self.assertIsNotNone(current['sets'][0]['deleted_at'])
        self.assertIsNone(current['sets'][-1]['planned_target_id'])
        edit = {k: v for k, v in recorded[0]['command'].items() if k != 'session_exercise_id'} | {'type': 'edit_set'}
        self.reject('validation_failed', self.operation(start['workout_id'], edit, revision))
        self.submit(self.add(start, revision=revision))
        self.assertEqual(self.current(start)['sets'][-1]['planned_target_id'], self.exercise['targets'][0]['target_id'])
        self.assertEqual(self.current(start)['sets'][-2]['set_id'], extra['command']['set_id'])

    def test_invalid_first_slot_wrong_exercise_and_snapshot_rejections_are_durable(self):
        missing = self.operation(command={'type': 'start_workout', 'plan_snapshot_id': 'missing'})
        self.assertEqual(self.reject('snapshot_unavailable', missing)['outcome'], 'rejected')
        start, _ = self.start()
        extra = self.add(start)
        extra['command'].update(planned_target_id=None, comparison_target_id=self.exercise['targets'][-1]['target_id'])
        self.reject('validation_failed', extra)
        invalid = self.add(start)
        invalid['command']['session_exercise_id'] = 'not-in-plan'
        self.reject('validation_failed', invalid)
        self.assertEqual(self.current(start)['server_revision'], 1)

    def test_completion_corrections_and_terminal_cancellation(self):
        start, _ = self.start()
        self.reject('validation_failed', self.operation(start['workout_id'], {'type': 'complete_workout'}, 1))
        warmup = self.add(start, warmup=True)
        self.submit(warmup)
        complete = self.operation(start['workout_id'], {'type': 'complete_workout'}, 2)
        self.submit(complete)
        completed_at = self.current(start)['completed_at']
        self.submit(self.operation(start['workout_id'], {'type': 'delete_set', 'set_id': warmup['command']['set_id']}, 3))
        current = self.current(start)
        self.assertEqual(current['status'], 'completed')
        self.assertEqual(current['completed_at'], completed_at)
        self.submit(self.add(start, revision=4))
        empty, _ = self.start()
        self.submit(self.operation(empty['workout_id'], {'type': 'cancel_empty_workout'}, 1))
        self.reject('validation_failed', self.add(empty, revision=2))
        self.assertEqual(self.current(empty)['status'], 'cancelled')

    def test_edit_retains_old_values_and_cannot_silently_change_target(self):
        start, _ = self.start()
        add = self.add(start)
        self.submit(add)
        edit = {k: v for k, v in add['command'].items() if k != 'session_exercise_id'}
        edit.update(type='edit_set', weight_g=75000, reps=8)
        self.submit(self.operation(start['workout_id'], edit, 2))
        current = self.current(start)
        self.assertEqual((current['sets'][0]['weight_g'], current['sets'][0]['reps']), (75000, 8))
        old = self.store.run(lambda db: db.get('workout_records', 'alice', revision_key(start['workout_id'], 2)))
        self.assertEqual(old['sets'][0]['weight_g'], 70000)
        self.assertEqual(current['sets'][0]['planned_target_id'], old['sets'][0]['planned_target_id'])
        self.reject('validation_failed', self.operation(start['workout_id'], edit | {'planned_target_id': None}, 3))
        warmup = edit | {'kind': 'warmup', 'planned_target_id': None, 'comparison_target_id': None}
        self.submit(self.operation(start['workout_id'], warmup, 3))
        self.assertEqual(self.current(start)['sets'][0]['kind'], 'warmup')
        self.submit(self.operation(start['workout_id'], edit, 4))
        self.assertEqual(self.current(start)['sets'][0]['kind'], 'working')

    def test_owner_session_and_receipt_isolation(self):
        start, _ = self.start()
        self.error('not_found', lambda: self.work('get_workout', self.bob, workout_id=start['workout_id']))
        self.error('not_found', lambda: self.work('get_plan', self.bob, plan_snapshot_id=self.plan['plan_snapshot_id']))
        self.error('not_found', lambda: self.work('get_receipt', self.bob, operation_id=start['operation_id']))
        other = self.add(start) | {'expected_owner_id': 'bob'}
        self.error('not_found', lambda: self.submit(other, self.bob))
        self.error('account_mismatch', lambda: self.submit(self.add(start), self.bob))
        self.assertEqual(self.work('list_workouts', self.bob)['items'], [])
        self.assertEqual(self.work('list_plans', self.bob)['items'], [])
        self.error('session_required', lambda: self.work('list_workouts', 'not-a-session'))

    def test_rollback_and_commit_with_lost_response_do_not_split_effect_and_receipt(self):
        start = self.operation()
        before = self.store.run(lambda db: db.get('workout_records', 'alice', 'meta'))
        real_put = Documents.put
        def fail_receipt(db, name, owner, key, value):
            if name == 'workout_records' and key.startswith('operation:'):
                raise RuntimeError('synthetic-before-commit')
            return real_put(db, name, owner, key, value)
        with patch.object(Documents, 'put', fail_receipt), self.assertRaises(RuntimeError):
            self.submit(start)
        self.error('not_found', lambda: self.current(start))
        self.error('not_found', lambda: self.work('get_receipt', operation_id=start['operation_id']))
        self.assertEqual(self.store.run(lambda db: db.get('workout_records', 'alice', 'meta')), before)
        run = self.store.run
        def lose_response(operation):
            run(operation)
            raise RuntimeError('synthetic-after-commit')
        with patch.object(self.store, 'run', lose_response), self.assertRaises(RuntimeError):
            self.submit(start)
        result = self.submit(start)
        self.assertEqual(result['server_revision'], 1)
        self.assertEqual(self.current(start)['server_revision'], 1)
        self.assertEqual(self.store.run(lambda db: db.get('workout_records', 'alice',
            revision_key(start['workout_id'], 1)))['server_revision'], 1)

    def test_bootstrap_and_history_pages_hold_immutable_versions(self):
        first, _ = self.start()
        second, _ = self.start()
        with patch('api.workouts.BOOTSTRAP_PAGE_SIZE', 2):
            bootstrap = self.work('bootstrap')
            self.assertIsNotNone(bootstrap['next_page_cursor'])
            self.submit(self.add(first))
            self.publish(self.propose(base=self.plan['program_version_id'], weight=65000))
            page = self.work('bootstrap', page_cursor=bootstrap['next_page_cursor'])
            self.assertEqual(page['high_watermark'], bootstrap['high_watermark'])
            self.assertEqual({item['data']['server_revision'] for item in page['items']}, {1})
            self.assertEqual(self.work('bootstrap', page_cursor=bootstrap['next_page_cursor']), page)
        history = self.work('list_workouts', limit=1)
        earlier = next(s for s in [first, second] if s['workout_id'] != history['items'][0]['workout_id'])
        old = self.current(earlier)
        self.submit(self.add(earlier, revision=old['server_revision'], warmup=True))
        page = self.work('list_workouts', limit=1, cursor=history['next_cursor'])
        self.assertEqual(page['items'][0], old)
        self.error('cursor_expired', lambda: self.work('list_workouts', self.bob, limit=1, cursor=history['next_cursor']))

    def test_change_feed_has_fixed_high_watermark_and_old_revisions(self):
        checkpoint = self.work('bootstrap')['checkpoint_cursor']
        start, _ = self.start()
        page = self.work('get_changes', cursor=checkpoint, limit=1)
        self.assertTrue(page['has_more'])
        self.assertEqual(page['items'][0]['workout']['server_revision'], 1)
        high = page['high_watermark']
        self.submit(self.add(start))
        self.assertEqual(self.work('get_changes', cursor=checkpoint, limit=1), page)
        rest = self.work('get_changes', cursor=page['next_cursor'], limit=1)
        self.assertEqual(rest['high_watermark'], high)
        self.assertFalse(rest['has_more'])
        following = self.work('get_changes', cursor=rest['next_cursor'])
        self.assertGreater(following['high_watermark'], high)
        self.assertEqual(following['items'][0]['workout']['server_revision'], 2)
        self.error('cursor_expired', lambda: self.work('get_changes', self.bob, cursor=checkpoint))

    def test_cursor_expiry_requires_bootstrap_but_receipts_remain(self):
        start, receipt = self.start()
        with patch('api.workouts.BOOTSTRAP_PAGE_SIZE', 1):
            bootstrap = self.work('bootstrap')
            self.clock += 901
            self.error('cursor_expired', lambda: self.work('bootstrap', page_cursor=bootstrap['next_page_cursor']))
        self.clock += 30 * 24 * 60 * 60
        self.alice = self.login('101', 'alice')
        self.error('cursor_expired', lambda: self.work('get_changes', cursor=bootstrap['checkpoint_cursor']))
        self.assertEqual(self.work('get_receipt', operation_id=start['operation_id']), receipt)

    def test_response_byte_budget_paginates_without_losing_items(self):
        first, _ = self.start()
        second, _ = self.start()
        budget = len(canonical_bytes(self.current(first))) + 20
        with patch('api.workouts.MAX_PAGE_BYTES', budget):
            page = self.work('list_workouts', limit=100)
            self.assertEqual(len(page['items']), 1)
            later = self.work('list_workouts', limit=100, cursor=page['next_cursor'])
            self.assertIsNone(later['next_cursor'])
            self.assertEqual({w['workout_id'] for w in page['items'] + later['items']}, {first['workout_id'], second['workout_id']})

    def test_rate_limit_does_not_hide_committed_receipts(self):
        start, receipt = self.start()
        self.store.run(lambda db: db.put('workout_records', 'alice', 'operation_window',
            {'minute': self.clock // 60, 'count': 240}))
        self.assertEqual(self.submit(start), receipt)
        add = self.add(start)
        self.reject('rate_limited', add)
        self.error('not_found', lambda: self.work('get_receipt', operation_id=add['operation_id']))
        self.clock += 60
        self.assertEqual(self.submit(add)['server_revision'], 2)

    def test_http_operations_receipts_get_limit_and_csrf(self):
        def request(method, path, body=None, **extra):
            headers = {'cookie': SESSION_COOKIE + '=' + self.alice, 'origin': self.auth.settings.origin,
                'x-csrf-token': self.auth.session(self.alice)['csrf_token'], 'content-type': 'application/json'}
            with patch('api.handler.application', return_value=self.auth):
                return handler({'httpMethod': method, 'path': path, 'headers': headers,
                    'body': canonical_bytes(body).decode() if body is not None else '', **extra})
        start = self.operation()
        self.assertEqual(request('POST', '/api/v1/sync/operations', start)['statusCode'], 200)
        invalid = self.add(start)
        invalid['command']['session_exercise_id'] = 'missing'
        response = request('POST', '/api/v1/sync/operations', invalid)
        self.assertEqual(response['statusCode'], 422)
        value = json.loads(response['body'])
        validate('ErrorResponse', value)
        self.assertEqual(value['error']['receipt']['outcome'], 'rejected')
        self.assertEqual(request('GET', '/api/v1/workouts', queryStringParameters={'limit': '1'})['statusCode'], 200)
        self.assertEqual(request('GET', '/api/v1/workouts', queryStringParameters={'limit': 'x'})['statusCode'], 400)
        self.assertEqual(request('POST', '/api/v1/sync/operations', start | {'schema_version': 2})['statusCode'], 409)
        with patch('api.handler.application', return_value=self.auth):
            response = handler({'httpMethod': 'POST', 'path': '/api/v1/sync/operations',
                'headers': {'cookie': SESSION_COOKIE + '=' + self.alice, 'content-type': 'application/json'},
                'body': canonical_bytes(self.operation()).decode()})
        self.assertEqual(response['statusCode'], 403)


class SqliteWorkoutTests(WorkoutCases, unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.prefix = 'gym_workouts_'
        for table in Store(self.engine, self.prefix).tables.values():
            table.create(self.engine)
        self.prepare_workouts()

    def tearDown(self):
        self.engine.dispose()
