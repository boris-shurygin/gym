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
            try:
                zone = analytics_zone(criteria['timezone'])
            except (ZoneInfoNotFoundError, FileNotFoundError, ValueError):
                raise ServiceError('validation_failed') from None
            if operation == 'get_series':
                criteria.update({k: value[k] for k in ('exercise_id', 'equipment_id', 'weight_convention')})
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
                 'end': end.isoformat(), 'version': 'stats-v1'})).hexdigest()
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
        lower = 'history:' + date_key(datetime.combine(start, time.min, zone).astimezone(timezone.utc).isoformat()) + ':'
        upper = 'history:' + date_key(datetime.combine(end, time.min, zone).astimezone(timezone.utc).isoformat()) + ':'
        report = {'history_revision': revision, 'analytics_version': 'stats-v1', 'freshness': 'fresh',
            'generated_at': timestamp(now), 'period_days': criteria['period_days'],
            'timezone': criteria['timezone'], 'start_date': start.isoformat(),
            'end_date_exclusive': end.isoformat(), 'complete': True}
        activity, points, set_count, rep_count, count = {}, [], 0, 0, 0
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
                metrics = working_metrics(workout)
                if not metrics:
                    continue
                day = datetime.fromisoformat(workout['started_at'].replace('Z', '+00:00')).astimezone(zone).date().isoformat()
                activity[day] = activity.get(day, 0) + 1
                for series, point in metrics:
                    set_count += point['working_set_count']
                    rep_count += point['rep_count']
                    if operation == 'get_series' and series == tuple(criteria[k]
                            for k in ('exercise_id', 'equipment_id', 'weight_convention')):
                        points.append(point)
            after = rows[-1][0]
            if len(canonical_bytes(points)) > MAX_MANIFEST_BYTES - 4096:
                raise ServiceError('unavailable')
        if any(value > MAX_INTEGER for value in (set_count, rep_count,
                *(point['volume_g_reps'] for point in points))):
            raise ServiceError('unavailable')
        if operation == 'get_summary':
            return report | {'workout_count': sum(activity.values()), 'working_set_count': set_count,
                'rep_count': rep_count, 'activity': [{'date': day, 'workout_count': value}
                    for day, value in sorted(activity.items())]}
        return report | {k: criteria[k] for k in ('exercise_id', 'equipment_id', 'weight_convention')} | {'points': points}
