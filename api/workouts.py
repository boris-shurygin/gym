"""Занятия и доставка неизменяемых операций через общий прикладной слой."""

import copy
from datetime import datetime, timezone
from uuid import uuid4

from api.auth import timestamp
from api.errors import ServiceError
from api.programs import RULES
from api.sync_state import MAX_INTEGER, append_event, metadata
from contracts.canonical import canonical_bytes, fingerprint
from contracts.validation import validate

OPERATIONS = {'list_exercises', 'list_plans', 'get_plan', 'list_workouts', 'get_workout',
    'submit_operation', 'get_receipt', 'get_changes', 'bootstrap'}
MANIFEST_TTL = 15 * 60
DELTA_TTL = 30 * 24 * 60 * 60
MAX_MANIFEST_BYTES = 512 * 1024
MAX_PAGE_BYTES = 900 * 1024
MAX_WORKOUT_BYTES = 512 * 1024
BOOTSTRAP_PAGE_SIZE = 100


def date_key(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def revision_key(workout_id, revision):
    return f'revision:{workout_id}:{revision:016d}'


class Workouts:
    def __init__(self, auth):
        self.auth, self.store = auth, auth.store

    def execute(self, operation, token, value):
        if operation not in OPERATIONS:
            raise ServiceError('unavailable')
        if operation == 'submit_operation' and type(value.get('schema_version')) is int and value['schema_version'] != 1:
            raise ServiceError('upgrade_required')
        validate(RULES[operation]['input'], value)
        def perform(db):
            owner = self.auth.principal(db, token)['user_id']
            if value.get('expected_owner_id', owner) != owner:
                raise ServiceError('account_mismatch')
            now = int(self.auth.now())
            if operation == 'list_exercises':
                limit = value.get('limit', 100)
                if value.get('cursor'):
                    manifest, offset = self._read_manifest(db, owner, value['cursor'], 'catalog', now)
                    if manifest['options']['limit'] != limit:
                        raise ServiceError('cursor_expired')
                    items = manifest['refs']
                else:
                    head = db.get('program_records', owner, 'head')
                    version = db.get('program_records', owner, 'version:' + head['id']) if head else None
                    catalog = {}
                    for template in version['content']['templates'] if version else []:
                        for exercise in template['exercises']:
                            entry = {key: exercise[key] for key in ('exercise_id', 'equipment_id', 'name',
                                'weight_convention', 'weight_step_g')}
                            catalog[tuple(entry[key] for key in ('exercise_id', 'equipment_id', 'weight_convention'))] = entry
                    items, offset = list(catalog.values()), 0
                    manifest = self._manifest(db, owner, 'catalog', items, now, options={'limit': limit}) if len(items) > limit else None
                end = min(len(items), offset + limit)
                result = {'items': items[offset:end], 'next_cursor': f"{manifest['id']}.{end}" if end < len(items) else None}
            elif operation == 'submit_operation':
                result = self._submit(db, owner, value, now)
            elif operation == 'list_plans':
                result = self._plans(db, owner, now)
            elif operation == 'get_plan':
                result = self._required(db, owner, 'plan:' + value['plan_snapshot_id'])
            elif operation == 'get_workout':
                result = self._required(db, owner, 'workout:' + value['workout_id'])
            elif operation == 'get_receipt':
                result = self._required(db, owner, 'operation:' + value['operation_id'])['receipt']
            elif operation == 'get_changes':
                result = self._changes(db, owner, value, now)
            elif operation == 'bootstrap':
                result = self._bootstrap(db, owner, value, now)
            else:
                result = self._history(db, owner, value, now)
            if '_failure' not in result:
                validate(RULES[operation]['output'], result)
            return result
        result = self.store.run(perform)
        # Окончательный отказ также должен успешно закоммитить квитанцию.
        if '_failure' in result:
            raise ServiceError(result['_failure'], receipt=result.get('receipt'))
        return result

    @staticmethod
    def _required(db, owner, key):
        value = db.get('workout_records', owner, key)
        if value is None:
            raise ServiceError('not_found')
        return value

    def _plans(self, db, owner, now):
        state = metadata(db, owner)
        head = db.get('program_records', owner, 'head')
        current = head['id'] if head else None
        cached = db.get('workout_records', owner, 'plans')
        if not cached or (cached['program_version_id'], cached['history_revision']) != (current, state['history_revision']):
            version = db.get('program_records', owner, 'version:' + current) if head else None
            items = []
            for template in version['content']['templates'] if version else []:
                plan_id = 'plan_' + uuid4().hex
                plan = {'plan_snapshot_id': plan_id, 'snapshot_schema_version': 1, 'owner_id': owner,
                    'program_version_id': current, 'template': copy.deepcopy(template),
                    'calculated_at': timestamp(now), 'history_revision': state['history_revision'],
                    'calculator_version': 'fixed-v1'}
                validate('PlanSnapshot', plan)
                db.put('workout_records', owner, 'plan:' + plan_id, plan)
                items.append(plan_id)
            cached = {'program_version_id': current, 'history_revision': state['history_revision'], 'ids': items}
            db.put('workout_records', owner, 'plans', cached)
            append_event(db, owner, {'kind': 'plans_changed', 'plan_ids': items}, now)
        return {'items': [self._required(db, owner, 'plan:' + key) for key in cached['ids']],
            'history_revision': state['history_revision'], 'freshness': 'fresh'}

    @staticmethod
    def _rejected(db, owner, operation, digest, code, revision, now):
        receipt = {'operation_id': operation['operation_id'], 'workout_id': operation['workout_id'],
            'outcome': 'rejected', 'code': code, 'observed_server_revision': revision,
            'rejected_at': timestamp(now)}
        validate('RejectedReceipt', receipt)
        db.put('workout_records', owner, 'operation:' + operation['operation_id'], {'digest': digest, 'receipt': receipt})
        db.put('workout_records', owner, 'rejection:' + operation['workout_id'] + ':' + operation['operation_id'],
            {'operation': operation, 'receipt': receipt})
        db.delete('workout_records', owner, 'pending:' + operation['operation_id'])
        return {'_failure': code, 'receipt': receipt}

    def _submit(self, db, owner, operation, now):
        key = operation['operation_id']
        digest = fingerprint('operation', operation)
        previous = db.get('workout_records', owner, 'operation:' + key)
        if previous:
            if previous['digest'] != digest:
                raise ServiceError('operation_id_reused')
            receipt = previous['receipt']
            return receipt if receipt['outcome'] == 'applied' else {'_failure': receipt['code'], 'receipt': receipt}
        pending = db.get('workout_records', owner, 'pending:' + key)
        if pending and pending['digest'] != digest:
            raise ServiceError('operation_id_reused')
        if pending is None:
            rate = db.get('workout_records', owner, 'operation_window')
            if not rate or rate['minute'] != now // 60:
                rate = {'minute': now // 60, 'count': 0}
            if rate['count'] >= 240:
                raise ServiceError('rate_limited')
            rate['count'] += 1
            db.put('workout_records', owner, 'operation_window', rate)
        workout_id = operation['workout_id']
        workout = db.get('workout_records', owner, 'workout:' + workout_id)
        observed = workout['server_revision'] if workout else 0
        reject = lambda code: self._rejected(db, owner, operation, digest, code, observed, now)
        predecessor = operation['precondition'].get('after_operation_id')
        if predecessor:
            parent = db.get('workout_records', owner, 'operation:' + predecessor)
            if parent:
                if parent['receipt']['workout_id'] != workout_id:
                    return reject('invalid_dependency')
                if parent['receipt']['outcome'] == 'rejected':
                    return reject('dependency_failed')
                expected = parent['receipt']['server_revision']
            else:
                # Сохрани только зависимость/хеш, без окончательной квитанции.
                # Это позволяет распознать цикл и не менять тело при повторе.
                seen, cursor = {key}, predecessor
                for _ in range(256):
                    if cursor in seen:
                        return reject('invalid_dependency')
                    seen.add(cursor)
                    ancestor = db.get('workout_records', owner, 'pending:' + cursor)
                    if not ancestor:
                        break
                    if ancestor['workout_id'] != workout_id:
                        return reject('invalid_dependency')
                    cursor = ancestor['after']
                else:
                    return reject('invalid_dependency')
                db.put('workout_records', owner, 'pending:' + key,
                    {'digest': digest, 'workout_id': workout_id, 'after': predecessor})
                return {'_failure': 'dependency_pending'}
        else:
            expected = operation['precondition']['server_revision']
        command = operation['command']
        if command['type'] != 'start_workout' and workout is None:
            raise ServiceError('not_found')
        if expected != observed:
            return reject('revision_conflict')
        if command['type'] == 'start_workout':
            plan = db.get('workout_records', owner, 'plan:' + command['plan_snapshot_id'])
            if plan is None:
                return reject('snapshot_unavailable')
            workout = {'workout_id': workout_id, 'owner_id': owner, 'server_revision': 1,
                'status': 'active', 'plan': plan, 'started_at': operation['client_occurred_at'],
                'completed_at': None, 'updated_at': timestamp(now), 'sets': []}
        else:
            if observed >= MAX_INTEGER:
                return reject('validation_failed')
            workout = copy.deepcopy(workout)
            if not self._mutate(workout, command, operation['client_occurred_at'], now):
                return reject('validation_failed')
            workout['server_revision'] += 1
            workout['updated_at'] = timestamp(now)
        try:
            started_key = date_key(workout['started_at'])
            completed_key = date_key(workout['completed_at']) if workout['completed_at'] else None
        except (ValueError, OverflowError):
            return reject('validation_failed')
        try:
            size = len(canonical_bytes(workout))
        except ValueError:
            return reject('validation_failed')
        if size > MAX_WORKOUT_BYTES:
            return reject('validation_failed')
        validate('Workout', workout)
        revision = workout['server_revision']
        db.put('workout_records', owner, 'workout:' + workout_id, workout)
        db.put('workout_records', owner, revision_key(workout_id, revision), workout)
        db.put('workout_records', owner, f'audit:{workout_id}:{revision:016d}',
            {'operation': operation, 'before_revision': observed, 'after_revision': revision,
             'received_at': timestamp(now)})
        reference = {'workout_id': workout_id, 'revision': revision, 'status': workout['status']}
        db.put('workout_records', owner, 'history:' + started_key + ':' + workout_id, reference)
        if workout['status'] == 'active':
            db.put('workout_records', owner, 'active:' + workout_id, reference)
        else:
            db.delete('workout_records', owner, 'active:' + workout_id)
        if workout['status'] == 'completed':
            db.put('workout_records', owner, 'completed:' + completed_key + ':' + workout_id, reference)
        seq = append_event(db, owner, {'kind': 'workout_changed', **reference}, now)
        state = metadata(db, owner)
        if state['history_revision'] >= MAX_INTEGER:
            raise ServiceError('unavailable')
        state['history_revision'] += 1
        db.put('workout_records', owner, 'meta', state)
        append_event(db, owner, {'kind': 'stats_invalidated', 'history_revision': state['history_revision']}, now)
        receipt = {'operation_id': key, 'workout_id': workout_id, 'outcome': 'applied',
            'server_revision': revision, 'change_seq': seq, 'accepted_at': timestamp(now)}
        validate('AppliedReceipt', receipt)
        db.put('workout_records', owner, 'operation:' + key, {'digest': digest, 'receipt': receipt})
        db.delete('workout_records', owner, 'pending:' + key)
        return receipt

    @staticmethod
    def _binding(workout, exercise_id, command, old=None):
        exercise = next((e for e in workout['plan']['template']['exercises']
            if e['template_exercise_id'] == exercise_id), None)
        if exercise is None:
            return False
        if old and old['kind'] == command['kind']:
            return (old['planned_target_id'], old['comparison_target_id']) == (
                command['planned_target_id'], command['comparison_target_id'])
        if command['kind'] == 'warmup':
            return command['planned_target_id'] is None and command['comparison_target_id'] is None
        occupied = {s['planned_target_id'] for s in workout['sets'] if s is not old
            and s['session_exercise_id'] == exercise_id and s['deleted_at'] is None and s['kind'] == 'working'}
        first = next((t['target_id'] for t in exercise['targets'] if t['target_id'] not in occupied), None)
        comparison = first if first is not None else exercise['targets'][-1]['target_id']
        return (command['planned_target_id'], command['comparison_target_id']) == (first, comparison)

    def _mutate(self, workout, command, occurred_at, now):
        if workout['status'] == 'cancelled':
            return False
        kind = command['type']
        if kind in {'complete_workout', 'cancel_empty_workout'}:
            has_results = any(s['deleted_at'] is None for s in workout['sets'])
            if workout['status'] != 'active' or (kind == 'complete_workout') != has_results:
                return False
            workout['status'] = 'completed' if has_results else 'cancelled'
            workout['completed_at'] = occurred_at if has_results else None
            return True
        old = next((s for s in workout['sets'] if s['set_id'] == command['set_id']), None)
        if kind == 'add_set':
            if old is not None or len(workout['sets']) >= 2000 or not self._binding(workout, command['session_exercise_id'], command):
                return False
            workout['sets'].append({k: v for k, v in command.items() if k != 'type'} | {'deleted_at': None})
        else:
            if old is None or old['deleted_at'] is not None:
                return False
            if kind == 'delete_set':
                old['deleted_at'] = timestamp(now)
            elif not self._binding(workout, old['session_exercise_id'], command, old):
                return False
            else:
                old.update({k: v for k, v in command.items() if k not in {'type', 'set_id'}})
        return True

    @staticmethod
    def _refs(db, owner, prefix, *, reverse=False, limit=None):
        result, after = [], None
        while limit is None or len(result) < limit:
            rows = db.scan('workout_records', owner, prefix, after=after, reverse=reverse,
                limit=min(200, limit - len(result)) if limit is not None else 200)
            if not rows:
                break
            result.extend(value for _, value in rows)
            after = rows[-1][0]
            if len(canonical_bytes(result)) > MAX_MANIFEST_BYTES:
                raise ServiceError('unavailable')
        return result

    def _resolve(self, db, owner, ref):
        kind = ref['kind']
        if kind == 'program':
            value = db.get('program_records', owner, 'version:' + ref['id'])
        elif kind == 'plan':
            value = db.get('workout_records', owner, 'plan:' + ref['id'])
        else:
            value = db.get('workout_records', owner, revision_key(ref['workout_id'], ref['revision']))
        if value is None:
            raise ServiceError('unavailable')
        return value

    def _manifest(self, db, owner, purpose, refs, now, *, options=None, checkpoint=None):
        if len(canonical_bytes(refs)) > MAX_MANIFEST_BYTES:
            raise ServiceError('unavailable')
        key = uuid4().hex
        result = {'id': key, 'purpose': purpose, 'version': 1, 'refs': refs,
            'expires': now + MANIFEST_TTL, 'high_watermark': metadata(db, owner)['seq'],
            'options': options, 'checkpoint': checkpoint}
        db.put('workout_records', owner, 'manifest:' + key, result)
        return result

    @staticmethod
    def _read_manifest(db, owner, token, purpose, now):
        parts = token.split('.')
        if len(parts) != 2 or len(parts[0]) != 32 or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 8:
            raise ServiceError('cursor_expired')
        value = db.get('workout_records', owner, 'manifest:' + parts[0])
        offset = int(parts[1])
        if not value or value['purpose'] != purpose or value['expires'] <= now or value['version'] != 1 or not 0 <= offset < len(value['refs']):
            raise ServiceError('cursor_expired')
        return value, offset

    def _history(self, db, owner, value, now):
        options = {'limit': value.get('limit', 100), 'status': value.get('status')}
        if 'cursor' in value:
            manifest, offset = self._read_manifest(db, owner, value['cursor'], 'history', now)
            if manifest['options'] != options:
                raise ServiceError('cursor_expired')
        else:
            refs = self._refs(db, owner, 'history:', reverse=True)
            refs = [ref | {'kind': 'workout'} for ref in refs if options['status'] is None or ref['status'] == options['status']]
            manifest, offset = self._manifest(db, owner, 'history', refs, now, options=options), 0
        items, end = self._page(db, owner, manifest['refs'], offset, options['limit'])
        return {'items': items,
            'next_cursor': f"{manifest['id']}.{end}" if end < len(manifest['refs']) else None}

    def _page(self, db, owner, refs, offset, limit, *, wrapped=False):
        items, size = [], 0
        for ref in refs[offset:offset + limit]:
            data = self._resolve(db, owner, ref)
            if wrapped and ref['kind'] == 'program':
                data = {'owner_id': owner, 'version': data}
            item = {'kind': ref['kind'], 'data': data} if wrapped else data
            size += len(canonical_bytes(item))
            if size > MAX_PAGE_BYTES:
                if not items:
                    raise ServiceError('unavailable')
                break
            items.append(item)
        return items, offset + len(items)

    @staticmethod
    def _checkpoint(db, owner, after, expires, *, high_watermark=None):
        key = uuid4().hex
        db.put('workout_records', owner, 'cursor:' + key,
            {'version': 1, 'after': after, 'expires': expires, 'high_watermark': high_watermark, 'next': {}})
        return key

    def _bootstrap(self, db, owner, value, now):
        if 'page_cursor' in value:
            manifest, offset = self._read_manifest(db, owner, value['page_cursor'], 'bootstrap', now)
        else:
            plans = self._plans(db, owner, now)['items']
            head = db.get('program_records', owner, 'head')
            refs = ([{'kind': 'program', 'id': head['id']}] if head else [])
            refs.extend({'kind': 'plan', 'id': plan['plan_snapshot_id']} for plan in plans)
            refs.extend(ref | {'kind': 'workout'} for ref in self._refs(db, owner, 'active:'))
            refs.extend(ref | {'kind': 'workout'} for ref in self._refs(db, owner, 'completed:', reverse=True, limit=30))
            checkpoint = self._checkpoint(db, owner, metadata(db, owner)['seq'], now + DELTA_TTL)
            manifest, offset = self._manifest(db, owner, 'bootstrap', refs, now, checkpoint=checkpoint), 0
        items, end = self._page(db, owner, manifest['refs'], offset, BOOTSTRAP_PAGE_SIZE, wrapped=True)
        return {'bootstrap_id': 'bs_' + manifest['id'], 'expires_at': timestamp(manifest['expires']),
            'high_watermark': manifest['high_watermark'], 'checkpoint_cursor': manifest['checkpoint'],
            'items': items,
            'next_page_cursor': f"{manifest['id']}.{end}" if end < len(manifest['refs']) else None}

    def _event(self, db, owner, row):
        result = {'seq': row['seq'], 'kind': row['kind']}
        if row['kind'] == 'workout_changed':
            result['workout'] = self._resolve(db, owner, row | {'kind': 'workout'})
        elif row['kind'] == 'program_published':
            result['program_version'] = self._resolve(db, owner, {'kind': 'program', 'id': row['version_id']})
        elif row['kind'] == 'plans_changed':
            result['plans'] = [self._resolve(db, owner, {'kind': 'plan', 'id': key}) for key in row['plan_ids']]
        else:
            result['history_revision'] = row['history_revision']
        return result

    def _changes(self, db, owner, value, now):
        cursor = db.get('workout_records', owner, 'cursor:' + value['cursor'])
        if cursor is None or cursor['version'] != 1 or cursor['expires'] <= now:
            raise ServiceError('cursor_expired')
        if cursor['high_watermark'] is None:
            cursor['high_watermark'] = metadata(db, owner)['seq']
        high = cursor['high_watermark']
        limit = value.get('limit', 100)
        rows = db.scan('workout_records', owner, 'event:', after=f"event:{cursor['after']:016d}", limit=limit)
        rows = [row for _, row in rows if row['seq'] <= high]
        if rows and rows[0]['recorded_at'] <= now - DELTA_TTL:
            raise ServiceError('cursor_expired')
        items, size = [], 0
        for row in rows:
            item = self._event(db, owner, row)
            size += len(canonical_bytes(item))
            if size > MAX_PAGE_BYTES:
                if not items:
                    raise ServiceError('unavailable')
                break
            items.append(item)
        last = items[-1]['seq'] if items else cursor['after']
        more = last < high
        next_key = str(limit)
        if next_key not in cursor['next']:
            cursor['next'][next_key] = self._checkpoint(db, owner, last if more else high,
                cursor['expires'] if more else now + DELTA_TTL, high_watermark=high if more else None)
        db.put('workout_records', owner, 'cursor:' + value['cursor'], cursor)
        return {'items': items, 'high_watermark': high,
            'next_cursor': cursor['next'][next_key], 'has_more': more}
