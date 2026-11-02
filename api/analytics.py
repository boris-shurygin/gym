"""Производные факты fixed-v1: пересчитывай из принятой истории, не меняй цели."""

import copy
import hashlib
from functools import lru_cache
from importlib import resources
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from api.auth import timestamp
from api.errors import ServiceError
from api.programs import RULES
from api.sync_state import MAX_INTEGER, metadata
from api.workouts import Workouts, date_key, revision_key, MAX_MANIFEST_BYTES
from contracts.canonical import canonical_bytes
from contracts.validation import validate

OPERATIONS = {'get_summary', 'get_series'}
PAGE_SIZE = 100


@lru_cache(maxsize=256)
def analytics_zone(name):
    # Используй закреплённую базу из пакета, а не разные базы времени ОС.
    if not re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*', name):
        raise ValueError('Некорректный часовой пояс')
    location = resources.files('tzdata.zoneinfo').joinpath(*name.split('/'))
    if not location.is_file():
        raise ZoneInfoNotFoundError(name)
    with location.open('rb') as source:
        return ZoneInfo.from_file(source, key=name)


def working_metrics(workout):
    """Только эффективные рабочие результаты завершённого занятия, в целых граммах."""
    if workout['status'] != 'completed':
        return []
    exercises = {e['template_exercise_id']: e for e in workout['plan']['template']['exercises']}
    grouped = {}
    for result in workout['sets']:
        if result['deleted_at'] is not None or result['kind'] != 'working':
            continue
        exercise = exercises[result['session_exercise_id']]
        key = tuple(exercise[k] for k in ('exercise_id', 'equipment_id', 'weight_convention'))
        item = grouped.setdefault(key, {'workout_id': workout['workout_id'],
            'started_at': workout['started_at'], 'max_weight_g': 0, 'max_weight_reps': 1,
            'volume_g_reps': 0, 'working_set_count': 0, 'rep_count': 0})
        if (result['weight_g'], result['reps']) > (item['max_weight_g'], item['max_weight_reps']):
            item['max_weight_g'], item['max_weight_reps'] = result['weight_g'], result['reps']
        item['volume_g_reps'] += result['weight_g'] * result['reps']
        item['working_set_count'] += 1
        item['rep_count'] += result['reps']
    return sorted(grouped.items())


def exercise_series_key(exercise, side=None):
    """Keep legacy load semantics and independent sides in distinct series."""
    if 'measurement' not in exercise:
        return 'v1|' + exercise['weight_convention']
    load = exercise['load']
    return '|'.join(('v2', exercise['measurement'], load['kind'],
        load.get('weight_convention', load['kind']), str(load.get('implement_count', 1)),
        side or 'single', exercise['set_structure']))


def working_metrics_v2(workout):
    if workout['status'] != 'completed':
        return []
    if workout['plan']['snapshot_schema_version'] == 1:
        return [((key[0], key[1], 'v1|' + key[2]), {
            **{k: v for k, v in point.items() if k != 'working_set_count'},
            'working_result_count': point['working_set_count'],
            'ordinary_set_count': point['working_set_count'], 'side_result_count': 0,
            'myo_series_count': 0, 'myo_component_count': 0, 'duration_seconds': 0,
            'max_scale_milli': None}) for key, point in working_metrics(workout)]
    exercises = {e['template_exercise_id']: e for e in workout['plan']['template']['exercises']}
    grouped, myo_seen = {}, set()
    for result in workout['sets']:
        if result['deleted_at'] is not None or result['kind'] != 'working':
            continue
        exercise = exercises[result['session_exercise_id']]
        load = exercise['load']
        external_reps = load['kind'] == 'external' and exercise['measurement'] == 'reps'
        key = (exercise['exercise_id'], exercise['equipment_id'], exercise_series_key(exercise, result['side']))
        item = grouped.setdefault(key, {
            'workout_id': workout['workout_id'], 'started_at': workout['started_at'],
            'max_weight_g': None, 'max_weight_reps': None,
            'volume_g_reps': 0 if external_reps else None, 'max_scale_milli': None,
            'working_result_count': 0, 'ordinary_set_count': 0, 'side_result_count': 0,
            'myo_series_count': 0, 'myo_component_count': 0, 'rep_count': 0, 'duration_seconds': 0})
        if result['weight_g'] is not None and (item['max_weight_g'] is None or
                (result['weight_g'], result['reps'] or 0) > (item['max_weight_g'], item['max_weight_reps'] or 0)):
            item['max_weight_g'], item['max_weight_reps'] = result['weight_g'], result['reps']
        if external_reps:
            item['volume_g_reps'] += result['weight_g'] * load['implement_count'] * result['reps']
        if result['scale_milli'] is not None:
            item['max_scale_milli'] = max(item['max_scale_milli'] or 0, result['scale_milli'])
        item['working_result_count'] += 1
        item['rep_count'] += result['reps'] or 0
        item['duration_seconds'] += result['duration_seconds'] or 0
        if result['side'] is not None:
            item['side_result_count'] += 1
        if exercise['set_structure'] == 'myo':
            item['myo_component_count'] += 1
            series = (exercise['template_exercise_id'], result['side'])
            if series not in myo_seen:
                item['myo_series_count'] += 1
                myo_seen.add(series)
        elif result['side'] is None:
            item['ordinary_set_count'] += 1
    return sorted(grouped.items())


class Analytics:
    def __init__(self, auth):
        self.auth, self.store = auth, auth.store

    def execute(self, operation, token, value):
        if operation not in OPERATIONS:
            raise ServiceError('unavailable')
        validate(RULES[operation]['input'], value)

        def perform(db):
            owner = self.auth.principal(db, token)['user_id']
            now = int(self.auth.now())
            profile = db.get('program_records', owner, 'profile') or {'analytics_timezone': 'UTC'}
            criteria = {'period_days': value.get('period_days', 30),
                'timezone': value.get('timezone', profile['analytics_timezone'])}
            if value.get('schema_version') == 2:
                criteria['schema_version'] = 2
            try:
                zone = analytics_zone(criteria['timezone'])
            except (ZoneInfoNotFoundError, FileNotFoundError, ValueError):
                raise ServiceError('validation_failed') from None
            if operation == 'get_series':
                series_fields = ('exercise_id', 'equipment_id',
                    'series_key' if value.get('schema_version') == 2 else 'weight_convention')
                criteria.update({k: value[k] for k in series_fields})
            revision = metadata(db, owner)['history_revision']
            if value.get('cursor'):
                manifest, offset = Workouts._read_manifest(db, owner, value['cursor'], 'stats', now)
                if manifest['options']['criteria'] != criteria:
                    raise ServiceError('cursor_expired')
                result = self._page(manifest['options']['report'], manifest['refs'], offset, manifest['id'])
                if result['history_revision'] != revision:
                    result['freshness'] = 'stale'
                return result
            end = datetime.fromtimestamp(now, zone).date() + timedelta(days=1)
            start = end - timedelta(days=criteria['period_days'])
            key = 'stats-cache:' + hashlib.sha256(b'gym-stats-cache-v1\0' + canonical_bytes(
                {'operation': operation, **criteria, 'start': start.isoformat(),
                 'end': end.isoformat(), 'version': 'stats-v2' if value.get('schema_version') == 2 else 'stats-v1'})).hexdigest()
            cached = db.get('workout_records', owner, key)
            if cached and cached['history_revision'] == revision:
                report = cached
            else:
                report = self._calculate(db, owner, operation, criteria, zone, start, end, revision, now)
                # Расчёт и проверка H коммитятся в одной SERIALIZABLE транзакции.
                # Конкурирующая правка H+1 вызывает abort/retry, а не ложную свежесть.
                if metadata(db, owner)['history_revision'] != revision:
                    raise ServiceError('unavailable')
                if len(canonical_bytes(report)) > MAX_MANIFEST_BYTES:
                    raise ServiceError('unavailable')
                db.put('workout_records', owner, key, report)
            if operation == 'get_summary':
                return report
            if len(report['points']) <= PAGE_SIZE:
                return report | {'next_cursor': None}
            envelope = {k: v for k, v in report.items() if k != 'points'}
            manifest = Workouts(self.auth)._manifest(db, owner, 'stats', report['points'], now,
                options={'criteria': criteria, 'report': envelope})
            return self._page(envelope, report['points'], 0, manifest['id'])

        def checked(db):
            result = perform(db)
            validate(RULES[operation]['output'], result)
            return result
        return self.store.run(checked)

    @staticmethod
    def _page(envelope, points, offset, identifier):
        end = min(len(points), offset + PAGE_SIZE)
        return copy.deepcopy(envelope) | {'points': copy.deepcopy(points[offset:end]),
            'next_cursor': f'{identifier}.{end}' if end < len(points) else None}

    @staticmethod
    def _calculate(db, owner, operation, criteria, zone, start, end, revision, now):
        v2 = criteria.get('schema_version') == 2
        lower = 'history:' + date_key(datetime.combine(start, time.min, zone).astimezone(timezone.utc).isoformat()) + ':'
        upper = 'history:' + date_key(datetime.combine(end, time.min, zone).astimezone(timezone.utc).isoformat()) + ':'
        report = {'history_revision': revision, 'analytics_version': 'stats-v2' if v2 else 'stats-v1', 'freshness': 'fresh',
            'generated_at': timestamp(now), 'period_days': criteria['period_days'],
            'timezone': criteria['timezone'], 'start_date': start.isoformat(),
            'end_date_exclusive': end.isoformat(), 'complete': True}
        activity, points, set_count, rep_count, count = {}, [], 0, 0, 0
        totals = {k: 0 for k in ('working_result_count', 'ordinary_set_count', 'side_result_count',
            'myo_series_count', 'myo_component_count', 'duration_seconds')}
        after = lower
        while True:
            rows = db.scan('workout_records', owner, 'history:', after=after, before=upper, limit=200)
            if not rows:
                break
            for _, reference in rows:
                count += 1
                if count > 10000:
                    # Не выдавай обрезанный набор за полный отчёт.
                    raise ServiceError('unavailable')
                if reference['status'] != 'completed':
                    continue
                workout = db.get('workout_records', owner,
                    revision_key(reference['workout_id'], reference['revision']))
                if workout is None:
                    raise ServiceError('unavailable')
                if not v2 and workout['plan']['snapshot_schema_version'] != 1:
                    raise ServiceError('upgrade_required')
                metrics = working_metrics_v2(workout) if v2 else working_metrics(workout)
                if not metrics:
                    continue
                day = datetime.fromisoformat(workout['started_at'].replace('Z', '+00:00')).astimezone(zone).date().isoformat()
                activity[day] = activity.get(day, 0) + 1
                for series, point in metrics:
                    if v2:
                        for field in totals:
                            totals[field] += point[field]
                    else:
                        set_count += point['working_set_count']
                    rep_count += point['rep_count']
                    if operation == 'get_series' and series == tuple(criteria[k]
                            for k in ('exercise_id', 'equipment_id', 'series_key' if v2 else 'weight_convention')):
                        points.append(point)
            after = rows[-1][0]
            if len(canonical_bytes(points)) > MAX_MANIFEST_BYTES - 4096:
                raise ServiceError('unavailable')
        if any(value > MAX_INTEGER for value in (set_count, rep_count, *totals.values(),
                *(value for point in points for value in point.values() if type(value) is int))):
            raise ServiceError('unavailable')
        if operation == 'get_summary':
            return report | (totals if v2 else {'working_set_count': set_count}) | {'workout_count': sum(activity.values()),
                'rep_count': rep_count, 'activity': [{'date': day, 'workout_count': value}
                    for day, value in sorted(activity.items())]}
        return report | {k: criteria[k] for k in ('exercise_id', 'equipment_id', 'series_key' if v2 else 'weight_convention')} | {'points': points}
