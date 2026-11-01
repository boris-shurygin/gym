"""Подключи только несекретные ссылки staging; production не наследует ключ."""
import json
from pathlib import Path
import re


def configure_diagnostics(payload, manifest, target):
    if target.NAME != 'staging' or not any(a['path'] == 'web/diagnostics.json' for a in manifest['artifacts']):
        return payload
    config = json.loads((Path(__file__).parent / 'staging-diagnostics.json').read_text(encoding='utf-8'))
    if set(config) != {'enabled', 'project', 'secret_id', 'secret_version_id'} or type(config['enabled']) is not bool:
        raise ValueError('Неизвестная конфигурация диагностики')
    if config['project'] != 'folder__b1ghdeobqrevsim6jg9o':
        raise ValueError('Monium project должен принадлежать staging')
    if not config['enabled']:
        if config['secret_id'] is not None or config['secret_version_id'] is not None:
            raise ValueError('Отключённая конфигурация не должна содержать секрет')
        return payload
    if any(not isinstance(config[k], str) or not re.fullmatch('[a-z0-9]{20}', config[k])
           for k in ('secret_id', 'secret_version_id')):
        raise ValueError('Нужна точная версия секрета Monium')
    payload['environment']['GYM_MONIUM_PROJECT'] = config['project']
    payload.setdefault('secrets', []).append({'id': config['secret_id'], 'versionId': config['secret_version_id'],
        'key': 'api_key', 'environmentVariable': 'GYM_MONIUM_API_KEY'})
    return payload
