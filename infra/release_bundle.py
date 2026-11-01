"""Собери и проверь готовые байты выпуска; не получай облачные credentials при сборке."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from infra.release_artifacts import payloads, validate_payloads
from infra.release_gate import REPOSITORY, WORKFLOW


ROOT = Path(__file__).resolve().parents[1]


def digest_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_archive(archive, metadata):
    archive = Path(archive)
    if archive.is_symlink() or archive.stat().st_size > 350 * 1024 * 1024 or digest_file(archive) != metadata['archive_sha256']:
        raise ValueError('Архив образа отличается от описания')
    with tarfile.open(archive) as package:
        members = package.getmembers()
        if (len(members) > 10000 or sum(m.size for m in members) > 500 * 1024 * 1024
                or any(m.name.startswith('/') or '\\' in m.name or '..' in m.name.split('/')
                       or not (m.isfile() or m.isdir()) for m in members)):
            raise ValueError('Недопустимый Docker archive')
        manifest = json.load(package.extractfile('manifest.json'))
        if len(manifest) != 1 or manifest[0]['RepoTags'] != ['gym-migrations:validation']:
            raise ValueError('Архив содержит неожиданные образы')
        config = package.extractfile(manifest[0]['Config']).read()
        if 'sha256:' + hashlib.sha256(config).hexdigest() != metadata['config_digest']:
            raise ValueError('Config digest образа отличается')


def verify_bundle(root, *, purpose, source_sha):
    root = Path(root)
    if root.is_symlink() or root.is_junction() or not re.fullmatch(r'[a-f0-9]{40}', source_sha):
        raise ValueError('Некорректный bundle/source SHA')
    for path in root.rglob('*'):
        if path.is_symlink() or path.is_junction() or (not path.is_file() and not path.is_dir()):
            raise ValueError('Bundle содержит ссылки/необычные файлы')
    files = payloads(root / 'payload')
    validate_payloads(files)
    metadata = json.loads((root / 'build.json').read_text(encoding='utf-8'))
    if (set(metadata) != {'format', 'purpose', 'source_sha', 'api', 'worker', 'files'}
            or metadata['format'] != 1 or metadata['purpose'] != purpose or metadata['source_sha'] != source_sha
            or metadata['worker']['source_sha'] != source_sha or metadata['worker']['purpose'] != purpose
            or metadata['api']['build_mode'] != 'linux' or metadata['api']['runtime'] != 'python312'
            or metadata['api']['entrypoint'] != 'index.handler'
            or metadata['api']['sha256'] != hashlib.sha256(files['api.zip']).hexdigest()):
        raise ValueError('Bundle не соответствует назначению/исходникам/готовому API')
    inventory = {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in files.items()}
    if metadata['files'] != inventory:
        raise ValueError('Список/байты payload изменились после сборки')
    allowed = {'build.json', 'worker/image.tar'} | {'payload/' + name for name in files}
    actual = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}
    if allowed != actual:
        raise ValueError('В bundle отсутствуют файлы или есть лишние')
    verify_archive(root / 'worker/image.tar', metadata['worker'])
    return metadata


def build_bundle(output, *, purpose='validation-only', source_sha=None):
    from scripts.build_api import build as build_api
    from scripts.build_worker_image import build as build_worker
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if source_sha is not None and sha != source_sha:
        raise ValueError('Checkout не соответствует проверенному SHA')
    if purpose == 'release':
        expected = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_REF': 'refs/heads/main',
                    'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_WORKFLOW_REF': REPOSITORY + '/' + WORKFLOW + '@refs/heads/main',
                    'GITHUB_SHA': sha, 'GITHUB_WORKFLOW_SHA': sha}
        if any(os.environ.get(k) != v for k, v in expected.items()):
            raise ValueError('Release-сборка разрешена только в доверенном workflow')
    elif purpose != 'validation-only':
        raise ValueError('Неизвестное назначение сборки')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Для новой сборки используй пустой output')
    output.mkdir(parents=True)
    api = build_api(output / 'api-build', run_smoke=True)
    api_metadata = json.loads(api.with_suffix('.json').read_text())
    payload = output / 'bundle/payload'
    payload.mkdir(parents=True)
    shutil.copyfile(api, payload / 'api.zip')
    subprocess.run(['npm', 'ci'], cwd=ROOT / 'web', check=True)
    subprocess.run(['npm', 'run', 'build'], cwd=ROOT / 'web', check=True,
                   env=os.environ | {'VITE_GYM_SOURCE_SHA': sha})
    shutil.copytree(ROOT / 'web/dist', payload / 'web')
    shutil.copyfile(ROOT / 'infra/gateway-release.json', payload / 'gateway.json')
    worker = build_worker(output / 'worker-build', purpose=purpose)
    (output / 'bundle/worker').mkdir()
    shutil.copyfile(output / 'worker-build/image.tar', output / 'bundle/worker/image.tar')
    files = payloads(payload)
    validate_payloads(files)
    metadata = {'format': 1, 'purpose': purpose, 'source_sha': sha, 'api': api_metadata, 'worker': worker,
                'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in files.items()}}
    (output / 'bundle/build.json').write_text(json.dumps(metadata, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    verify_bundle(output / 'bundle', purpose=purpose, source_sha=sha)
    print(json.dumps({'source_sha': sha, 'purpose': purpose, 'bundle': str(output / 'bundle')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path, required=True)
    parser.add_argument('--purpose', choices=['validation-only', 'release'], default='validation-only')
    parser.add_argument('--source-sha')
    args = parser.parse_args()
    build_bundle(args.output_directory, purpose=args.purpose, source_sha=args.source_sha)


if __name__ == '__main__':
    main()
