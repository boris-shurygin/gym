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


def requires_schema2(value):
    """Определи наличие нового представления, не преобразуя сохранённый документ."""
    if isinstance(value, list):
        return any(requires_schema2(item) for item in value)
    if not isinstance(value, dict):
        return False
    return (value.get('content_schema_version') == 2
        or value.get('snapshot_schema_version') == 2
        or value.get('schema_version') == 2
        or value.get('analytics_version') == 'stats-v2'
        or isinstance(value.get('scopes'), list) and 'program:publish_direct' in value['scopes']
        or isinstance(value.get('authorization'), dict) and value['authorization'].get('kind') == 'delegated_agent'
        or {'measurement', 'load', 'set_structure'} <= value.keys()
        or any(requires_schema2(item) for item in value.values()))


def validate_values_v2(exercise, value, *, target=False):
    """Проверь единицы и сторону факта по неизменяемым метаданным упражнения."""
    repetition = 'suggested_reps' if target else 'reps'
    if exercise['measurement'] == 'reps':
        if value[repetition] is None or value['duration_seconds'] is not None:
            raise ValueError('Ожидаются повторения, а не длительность')
    elif value[repetition] is not None or value['duration_seconds'] is None:
        raise ValueError('Ожидается длительность, а не повторения')
    load = exercise['load']['kind']
    if ((value['weight_g'] is not None) != (load == 'external')
            or (value['scale_milli'] is not None) != (load == 'machine_scale')):
        raise ValueError('Значения нагрузки не соответствуют её виду')
    if not target and ((value['side'] is None) != (exercise['sides'] == 'single')):
        raise ValueError('Сторона результата не соответствует упражнению')


def _semantics(value):
    if isinstance(value, list):
        for item in value:
            _semantics(item)
    elif isinstance(value, dict):
        if {'target_id','suggested_reps','rep_min','rep_max'} <= value.keys():
            lo, hi = value['rep_min'], value['rep_max']
            reps = value['suggested_reps']
            if ((lo is None) != (hi is None)
                    or lo is not None and (reps is None or not lo <= reps <= hi)):
                raise ValueError('Рекомендуемые повторения вне полного диапазона')
        if {'measurement', 'load', 'set_structure'} <= value.keys():
            load = value['load']
            if load['kind'] == 'external' and load['weight_convention'] != 'per_implement_kg' and load['implement_count'] != 1:
                raise ValueError('Множитель снарядов применим только к весу одного снаряда')
            rest = value['rest_seconds']
            if rest is not None and rest['min'] > rest['max']:
                raise ValueError('Неверный диапазон отдыха')
            if value['set_structure'] == 'myo' and (value['measurement'] != 'reps'
                    or 'targets' in value and len(value['targets']) < 2):
                raise ValueError('Миосерия содержит активацию и короткий подход с повторениями')
            for target in value.get('targets', []):
                validate_values_v2(value, target, target=True)
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
                version2 = value['plan']['snapshot_schema_version'] == 2
                if version2:
                    validate_values_v2(exercise, result)
                targets = exercise['targets']
                ids = {t['target_id'] for t in targets}
                if result['kind'] == 'working':
                    target = result['planned_target_id']
                    if target is not None and target not in ids:
                        raise ValueError('Плановой позиции нет в снимке')
                    if target is None and result['comparison_target_id'] != targets[-1]['target_id']:
                        raise ValueError('Дополнительный подход сравнивается с последней целью')
                    key = (result['session_exercise_id'], target, result.get('side'))
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
