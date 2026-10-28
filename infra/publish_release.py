"""Опубликуй проверенный ready bundle после независимого допуска source/run и OIDC."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from infra.object_store import ImmutableStore
from infra.release_artifacts import create_manifest, publish
from infra.release_bundle import verify_bundle
from infra.release_gate import context_from_github, publisher_token


REPOSITORY = 'cr.yandex/crpe91o8ka9e03n3n90c/gym-migrations'
BUCKET = 'gym-delivery-b1ghmudqihbvpnscd2fq'
ROOT = Path(__file__).resolve().parents[1]


def docker(args, *, env=None):
    result = subprocess.run(['docker', *args], env=env, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError('Docker-публикация не подтверждена; сырые stdout/stderr не выводятся')
    return result.stdout.strip()


def publish_bundle(root, context, token):
    metadata = verify_bundle(root, purpose='release', source_sha=context['source_sha'])
    # Загрузка и проверка конфигурации до выдачи credentials Docker helper.
    docker(['load', '-i', str(Path(root) / 'worker/image.tar')])
    config = docker(['image', 'inspect', 'gym-migrations:validation', '--format', '{{.Id}}'])
    if config != metadata['worker']['config_digest']:
        raise ValueError('Загружен другой образ')
    tag = REPOSITORY + ':release-' + context['release_id']
    with tempfile.TemporaryDirectory(prefix='gym-docker-credentials-') as directory:
        # Файл хранит только имя helper; IAM передаётся в памяти дочернего процесса.
        Path(directory, 'config.json').write_text(json.dumps({'credHelpers': {'cr.yandex': 'gym'}}))
        env = os.environ | {'DOCKER_CONFIG': directory, 'GYM_REGISTRY_IAM_TOKEN': token,
                            'PATH': str(ROOT / 'infra') + os.pathsep + os.environ['PATH']}
        docker(['tag', config, tag], env=env)
        docker(['push', tag], env=env)
        digests = json.loads(docker(['image', 'inspect', tag, '--format', '{{json .RepoDigests}}'], env=env))
    images = [item for item in digests if re.fullmatch(re.escape(REPOSITORY) + r'@sha256:[a-f0-9]{64}', item)]
    if len(images) != 1:
        raise ValueError('Digest опубликованного образа не подтверждён')
    manifest = create_manifest(Path(root) / 'payload', context['source_sha'], context['run_id'], context['attempt'],
                               images[0], metadata['worker']['revision'])
    result = publish(manifest, Path(root) / 'payload', ImmutableStore(token, BUCKET))
    return result | {'image': images[0], 'purpose': 'published-artifacts-only'}


def main():
    context = context_from_github()
    bundle = ROOT / '.local/downloaded-release'
    verify_bundle(bundle, purpose='release', source_sha=context['source_sha'])
    # Секрет не появляется до проверки bundle/источника, не выводится и не пишется на диск.
    result = publish_bundle(bundle, context, publisher_token(context))
    Path(ROOT / '.local/published-release.json').write_text(json.dumps(result, indent=2) + '\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write('Опубликованы артефакты выпуска `' + context['release_id'] + '`.\n\n')
        summary.write('Object Storage: `releases/' + context['release_id'] + '/manifest.json`.\n\n')
        summary.write('Образ миграций: `' + result['image'] + '`.\n\n')
        summary.write('Развёртывание приложения ещё не выполняется (P04.3.2).\n')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Публикация остановлена: ' + type(error).__name__)
        raise SystemExit(1)
