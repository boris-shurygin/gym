"""Принятый canonical-json-v1 Gym для строго проверенных входных DTO."""

import hashlib
import json

MAX_INTEGER = 9_007_199_254_740_991
MAX_BYTES = 1_048_576
PREFIXES = {
    'operation': b'gym:sync-operation:v1\n',
    'proposal': b'gym:program-change:v1\n',
    'request': b'gym:request:v1\n',
}


def _integer(raw):
    value = int(raw)
    if abs(value) > MAX_INTEGER:
        raise ValueError('Целое значение выходит за точный диапазон JSON v1')
    return value


def _no_float(raw):
    raise ValueError('В канонических запросах v1 дробные JSON-числа запрещены')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повторяющееся имя JSON-поля')
        result[key] = value
    return result


def _check(value, depth=0):
    if depth > 64:
        raise ValueError('Превышена глубина JSON')
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > MAX_INTEGER:
            raise ValueError('Превышен точный диапазон целых')
    elif type(value) is str:
        value.encode('utf-8', errors='strict')
    elif type(value) is list:
        for item in value:
            _check(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key.isascii():
                raise ValueError('Имена полей канонического DTO должны быть ASCII')
            _check(key, depth + 1)
            _check(item, depth + 1)
    else:
        raise ValueError('Недопустимый тип канонического JSON')


def canonical_bytes(value):
    _check(value)
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(result) > MAX_BYTES:
        raise ValueError('Превышен предел тела запроса 1 MiB')
    return result


def parse_request(raw):
    if isinstance(raw, str):
        raw = raw.encode('utf-8', errors='strict')
    if len(raw) > MAX_BYTES:
        raise ValueError('Превышен предел тела запроса 1 MiB')
    try:
        value = json.loads(raw.decode('utf-8', errors='strict'), parse_int=_integer,
                           parse_float=_no_float, parse_constant=_no_float,
                           object_pairs_hook=_pairs)
        canonical_bytes(value)
        return value
    except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise ValueError('Некорректный JSON v1') from error


def fingerprint(kind, value):
    """Сначала проверь DTO по схеме; хеш не подтверждает личность/одобрение."""
    return hashlib.sha256(PREFIXES[kind] + canonical_bytes(value)).hexdigest()
