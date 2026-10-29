"""Единая последовательность изменений владельца; вызывай внутри транзакции."""

from api.errors import ServiceError

MAX_INTEGER = 9007199254740991


def metadata(db, owner):
    return db.get('workout_records', owner, 'meta') or {'seq': 0, 'history_revision': 0}


def append_event(db, owner, event, now):
    state = metadata(db, owner)
    if state['seq'] >= MAX_INTEGER:
        raise ServiceError('unavailable')
    state['seq'] += 1
    db.put('workout_records', owner, 'meta', state)
    db.put('workout_records', owner, f"event:{state['seq']:016d}",
        {'seq': state['seq'], 'recorded_at': now, **event})
    return state['seq']
