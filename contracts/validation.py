"""Проверь форму DTO и локальные инварианты; права и транзакции проверяет сервер."""

from datetime import date, datetime
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from contracts.canonical import canonical_bytes

ROOT = Path(__file__).resolve().parent
SCHEMA = json.loads((ROOT / 'schema.json').read_text(encoding='utf-8'))
FORMATS = FormatChecker()


@FORMATS.checks('date-time', raises=(ValueError, TypeError))
def timestamp(value):
    if not isinstance(value, str):
        return True
    return datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None


@FORMATS.checks('date', raises=(ValueError, TypeError))
def calendar_date(value):
    if not isinstance(value, str):
        return True
    date.fromisoformat(value)
    return True


def validator(name):
    return Draft202012Validator({'$schema': SCHEMA['$schema'], '$ref': '#/$defs/' + name, '$defs': SCHEMA['$defs']}, format_checker=FORMATS)


def _unique(items, key):
    if len(items) != len({item[key] for item in items}):
        raise ValueError(f'Повторяется {key}')


def _semantics(value):
    if isinstance(value, list):
        for item in value:
            _semantics(item)
    elif isinstance(value, dict):
        if {'target_id','suggested_reps','rep_min','rep_max'} <= value.keys():
            lo, hi = value['rep_min'], value['rep_max']
            if (lo is None) != (hi is None) or lo is not None and not lo <= value['suggested_reps'] <= hi:
                raise ValueError('Рекомендуемые повторения вне полного диапазона')
        for field, key in [('templates','template_id'),('exercises','template_exercise_id'),('targets','target_id'),('sets','set_id')]:
            if field in value:
                _unique(value[field], key)
        if value.get('kind') == 'working' and value.get('planned_target_id') is not None and value.get('comparison_target_id') != value['planned_target_id']:
            raise ValueError('Плановый подход сравнивается со своей целью')
        if 'operation_id' in value and 'precondition' in value:
            if value['precondition'].get('after_operation_id') == value['operation_id']:
                raise ValueError('Операция не может зависеть от себя')
        if {'workout_id','plan','sets'} <= value.keys():
            exercises = {e['template_exercise_id']: e for e in value['plan']['template']['exercises']}
            occupied = set()
            for result in value['sets']:
                exercise = exercises.get(result['session_exercise_id'])
                if exercise is None:
                    raise ValueError('Упражнения нет в снимке')
                targets = exercise['targets']
                ids = {t['target_id'] for t in targets}
                if result['kind'] == 'working':
                    target = result['planned_target_id']
                    if target is not None and target not in ids:
                        raise ValueError('Плановой позиции нет в снимке')
                    if target is None and result['comparison_target_id'] != targets[-1]['target_id']:
                        raise ValueError('Дополнительный подход сравнивается с последней целью')
                    key = (result['session_exercise_id'], target)
                    if target is not None and result['deleted_at'] is None:
                        if key in occupied:
                            raise ValueError('Плановая позиция занята дважды')
                        occupied.add(key)
        if 'error' in value and isinstance(value['error'], dict):
            error = value['error']
            if error['receipt'] is not None and error['receipt']['code'] != error['code']:
                raise ValueError('Код квитанции и ошибки различается')
        for item in value.values():
            _semantics(item)


def validate(name, value):
    canonical_bytes(value)
    validator(name).validate(value)
    _semantics(value)
