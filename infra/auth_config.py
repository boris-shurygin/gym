"""Несекретные ссылки OAuth. Production не наследует конфигурацию staging."""

import json
from pathlib import Path
import re


def configure_api(payload, manifest, target):
    if target.NAME != 'staging' or manifest['migration']['revision'] not in {'0002_accounts_programs', '0003_workouts_sync'}:
        return payload
    config = json.loads((Path(__file__).parent / 'staging-auth.json').read_text(encoding='utf-8'))
    if set(config) != {'enabled', 'client_id', 'secret_id', 'secret_version_id'} or type(config['enabled']) is not bool:
        raise ValueError('Неизвестный формат конфигурации OAuth')
    if not config['enabled']:
        if any(config[key] is not None for key in config if key != 'enabled'):
            raise ValueError('Отключённая конфигурация должна быть пустой')
        return payload
    if (not isinstance(config['client_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', config['client_id'])
            or any(not isinstance(config[key], str) or not re.fullmatch(r'[a-z0-9]{20}', config[key])
                   for key in ('secret_id', 'secret_version_id'))):
        raise ValueError('Укажи Client ID и точную версию секрета окружения')
    payload['environment'].update(GYM_ORIGIN=target.ORIGIN, GYM_YANDEX_CLIENT_ID=config['client_id'])
    payload['secrets'] = [{'id': config['secret_id'], 'versionId': config['secret_version_id'],
        'key': 'client_secret', 'environmentVariable': 'GYM_YANDEX_CLIENT_SECRET'}]
    return payload
