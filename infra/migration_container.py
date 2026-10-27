"""План/развёртывание приватного миграционного контейнера только в staging."""

import argparse
import hashlib
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
    if ticket is not None:
        keys = {'job_id', 'owner', 'generation', 'not_before', 'evidence_sha256'}
        if (not isinstance(ticket, dict) or set(ticket) != keys
                or not isinstance(ticket['job_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ticket['job_id'])
                or not isinstance(ticket['owner'], str) or not re.fullmatch(r'[a-f0-9]{32}', ticket['owner'])
                or type(ticket['generation']) is not int or ticket['generation'] < 1
                or type(ticket['not_before']) is not int or ticket['not_before'] < 0
                or not isinstance(ticket['evidence_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', ticket['evidence_sha256'])):
            raise ValueError('Некорректное подтверждение recovery до развёртывания')
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


def recovery_evidence(evidence, ticket, revisions, image):
    keys = {'old_revision', 'observed_obsolete_at', 'execution_timeout_seconds', 'quiescence_seconds',
            'owner', 'generation', 'job_id'}
    if (not isinstance(evidence, dict) or set(evidence) != keys
            or type(evidence['observed_obsolete_at']) is not int
            or not 0 <= evidence['observed_obsolete_at'] <= time.time()
            or evidence['execution_timeout_seconds'] != 120 or evidence['quiescence_seconds'] != 180
            or ticket['not_before'] < evidence['observed_obsolete_at'] + 180
            or any(evidence[key] != ticket[key] for key in ('job_id', 'owner', 'generation'))):
        raise ValueError('Свидетельство не подтверждает требуемое ожидание и владельца')
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    old = next((r for r in revisions if r['id'] == evidence['old_revision']), None)
    if (ticket['evidence_sha256'] != digest or not old or old['container_id'] != CONTAINER
            or old['status'] != 'OBSOLETE' or old['execution_timeout'] != '120s'
            or old['image']['image_digest'] != image.split('@')[1]
            or old['service_account_id'] != load_config()['accounts']['migrations']
            or 'http' not in old.get('runtime', {})):
        raise ValueError('Свидетельство/прежняя ревизия не соответствуют staging worker')


def plan(yc, image, env, *, evidence=None):
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
    if env['GYM_WORKER_MODE'] == 'recovery':
        recovery_evidence(evidence, ticket, revisions, image)
    active = next((r for r in revisions if r['status'] == 'ACTIVE'), None)
    matches = (active is not None and active['image']['image_digest'] == image.split('@')[1]
               and active['image'].get('environment', {}) == env
               and active['service_account_id'] == account
               and active['execution_timeout'] == '120s' and active.get('concurrency', '1') == '1'
               and active['resources']['memory'] == '536870912'
               and str(active['resources']['cores']) == '1'
               and str(active['resources'].get('core_fraction')) == '100'
               and active.get('metadata_options') == {'gce_http_endpoint': 'ENABLED', 'aws_v1_http_endpoint': 'DISABLED'}
               and 'http' in active.get('runtime', {})
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


def deploy(yc, image, env, *, apply=False, evidence=None):
    result = plan(yc, image, env, evidence=evidence)
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
    confirmed = plan(yc, image, env, evidence=evidence)
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
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--crash-probe', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--yc', default=str(ROOT / '.local/bin/yc.exe'))
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Этот staging-инструмент запускает оператор вне CI')
    ticket = json.loads(args.ticket.read_text()) if args.ticket else None
    env = environment(load_config(), args.release, args.prefix, args.mode, ticket, args.crash_probe)
    evidence = json.loads(args.evidence.read_text()) if args.evidence else None
    print(json.dumps(deploy(Yc(args.yc), args.image, env, apply=args.apply, evidence=evidence), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Развёртывание остановлено: ' + type(error).__name__)
        raise SystemExit(1)
