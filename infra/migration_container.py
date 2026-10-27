"""План/развёртывание приватного миграционного контейнера только в staging."""

import argparse
import json
import os
from pathlib import Path
import re
import time
import urllib.request

from infra.object_store import NoRedirect
from infra.ydb_access import ROOT, Yc, load_config


CONTAINER = 'bba0n53on27kvvqler17'
REGISTRY = 'crpe91o8ka9e03n3n90c'
URL = 'https://bba0n53on27kvvqler17.containers.yandexcloud.net'
IMAGE_PATTERN = r'cr\.yandex/crpe91o8ka9e03n3n90c/gym-migrations@sha256:[a-f0-9]{64}'


def cloud(yc, *args):
    return yc.run(*args, '--endpoint', 'api.cloud.yandex.net:443')


def environment(config, release, prefix, mode, ticket=None, crash=False):
    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', release)
            or not (prefix == 'gym_' or re.fullmatch(r'gym_worker_probe_[a-f0-9]{12}_', prefix))
            or mode not in {'worker', 'bootstrap', 'recovery'}):
        raise ValueError('Некорректная конфигурация worker')
    if (mode == 'recovery') != (ticket is not None):
        raise ValueError('Recovery требует ticket, другие режимы его не принимают')
    if crash and (mode != 'worker' or not prefix.startswith('gym_worker_probe_')):
        raise ValueError('Crash допустим только для синтетического staging probe')
    result = {'GYM_YDB_ENDPOINT': config['endpoint'], 'GYM_YDB_DATABASE': config['database'],
              'GYM_YDB_AUTH': 'metadata', 'GYM_RELEASE_ID': release, 'GYM_TABLE_PREFIX': prefix,
              'GYM_WORKER_MODE': mode}
    if ticket:
        result['GYM_RECOVERY_TICKET'] = json.dumps(ticket, sort_keys=True, separators=(',', ':'))
    if crash:
        result['GYM_CRASH_AFTER_ACQUIRE'] = '1'
    return result


def plan(yc, image, env):
    if not re.fullmatch(IMAGE_PATTERN, image):
        raise ValueError('Укажи образ миграций нашего реестра по digest')
    config = load_config()
    ticket = json.loads(env['GYM_RECOVERY_TICKET']) if 'GYM_RECOVERY_TICKET' in env else None
    canonical = environment(config, env['GYM_RELEASE_ID'], env['GYM_TABLE_PREFIX'], env['GYM_WORKER_MODE'],
                            ticket, env.get('GYM_CRASH_AFTER_ACQUIRE') == '1')
    if env != canonical:
        raise ValueError('Окружение не соответствует фиксированной конфигурации staging')
    container = cloud(yc, 'serverless', 'container', 'get', '--id', CONTAINER)
    if (container['folder_id'] != config['folder_id'] or container['name'] != 'gym-migrations'
            or container.get('labels', {}).get('gym-project') != 'gym'):
        raise ValueError('Контейнер не соответствует Gym staging')
    bindings = cloud(yc, 'serverless', 'container', 'list-access-bindings', '--id', CONTAINER)
    if any(binding['subject'].get('type') == 'system' for binding in bindings):
        raise ValueError('Публичный доступ к административному контейнеру запрещён')
    account = config['accounts']['migrations']
    bindings = yc.run('container', 'registry', 'list-access-bindings', '--id', REGISTRY)
    own = [binding for binding in bindings if binding['subject'].get('id') == account]
    if any(binding['role_id'] != 'container-registry.images.puller' for binding in own):
        raise ValueError('У мигратора лишние прямые роли в реестре')
    revisions = cloud(yc, 'serverless', 'container', 'revision', 'list', '--container-id', CONTAINER)
    active = next((r for r in revisions if r['status'] == 'ACTIVE'), None)
    matches = (active is not None and active['image']['image_digest'] == image.split('@')[1]
               and active['image'].get('environment', {}) == env
               and active['service_account_id'] == account
               and active['execution_timeout'] == '120s' and active.get('concurrency', '1') == '1'
               and active['resources']['memory'] == '536870912'
               and str(active['resources']['cores']) == '1'
               and active.get('log_options', {}).get('disabled') is True
               and active.get('provision_policy', {}).get('min_instances', '0') == '0'
               and active.get('scaling_policy', {}).get('zone_instances_limit') == '1'
               and active.get('scaling_policy', {}).get('zone_requests_limit') == '1')
    return {'add_registry_puller': not own, 'deploy': not matches,
            'active_revision': active['id'] if active else None,
            'container_id': CONTAINER, 'image': image, 'environment': env}


def api(yc, url, *, payload=None):
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': 'Bearer ' + yc.token(), 'Content-Type': 'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
        return json.load(response)


def deploy(yc, image, env, *, apply=False):
    result = plan(yc, image, env)
    if not apply:
        return result
    config = load_config()
    if result['add_registry_puller']:
        yc.run('container', 'registry', 'add-access-binding', '--id', REGISTRY,
               '--role', 'container-registry.images.puller', '--service-account-id', config['accounts']['migrations'])
    if not result['deploy']:
        return result
    payload = {'containerId': CONTAINER, 'description': 'Gym migration worker; operator staging validation',
               'resources': {'memory': '536870912', 'cores': '1', 'coreFraction': '100'},
               'executionTimeout': '120s', 'concurrency': '1',
               'serviceAccountId': config['accounts']['migrations'],
               'imageSpec': {'imageUrl': image, 'environment': env},
               'provisionPolicy': {'minInstances': '0'}, 'logOptions': {'disabled': True},
               'scalingPolicy': {'zoneInstancesLimit': '1', 'zoneRequestsLimit': '1'},
               'runtime': {'http': {}},
               'metadataOptions': {'gceHttpEndpoint': 'ENABLED', 'awsV1HttpEndpoint': 'DISABLED'}}
    operation = api(yc, 'https://serverless-containers.api.cloud.yandex.net/containers/v1/revisions:deploy', payload=payload)
    for _ in range(40):
        if operation.get('done'):
            break
        time.sleep(2)
        operation = yc.run('operation', 'get', operation['id'])
    if not operation.get('done') or operation.get('error'):
        raise RuntimeError('Развёртывание не подтверждено; проверь операцию/реальную ревизию перед повтором')
    confirmed = plan(yc, image, env)
    if confirmed['deploy'] or confirmed['add_registry_puller']:
        raise RuntimeError('Фактическая ревизия отличается от конфигурации')
    return confirmed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--prefix', default='gym_')
    parser.add_argument('--mode', choices=['worker', 'bootstrap', 'recovery'], default='worker')
    parser.add_argument('--ticket', type=Path)
    parser.add_argument('--crash-probe', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--yc', default=str(ROOT / '.local/bin/yc.exe'))
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Этот staging-инструмент запускает оператор вне CI')
    ticket = json.loads(args.ticket.read_text()) if args.ticket else None
    env = environment(load_config(), args.release, args.prefix, args.mode, ticket, args.crash_probe)
    print(json.dumps(deploy(Yc(args.yc), args.image, env, apply=args.apply), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Развёртывание остановлено: ' + type(error).__name__)
        raise SystemExit(1)
