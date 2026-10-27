"""Проверь полный набор артефактов; публикуй манифест последним."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from jsonschema import Draft202012Validator

from contracts.canonical import canonical_bytes, parse_request
from infra.object_store import ImmutableStore, MAX_OBJECT

SCHEMA = json.loads((Path(__file__).parent / 'release.schema.json').read_text(encoding='utf-8'))
TYPES = {
    '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.json': 'application/json',
    '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml',
    '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
}
MAX_BUNDLE = 256 * 1024 * 1024
DELIVERY_BUCKET = 'gym-delivery-b1ghmudqihbvpnscd2fq'


def valid_path(name):
    path = PurePosixPath(name)
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', name) or path.is_absolute() or any(p in {'', '.', '..'} for p in name.split('/')):
        raise ValueError('Некорректный путь артефакта')
    if name not in {'api.zip', 'gateway.json'}:
        if not name.startswith('web/') or len(path.parts) < 2 or any(p.startswith('.') for p in path.parts):
            raise ValueError('Неожиданный файл в наборе выпуска')
        if path.parts[1].lower() in {'api', 'auth', 'oauth', 'mcp'}:
            raise ValueError('Служебный маршрут не может быть публичным ресурсом PWA')
        if path.suffix not in TYPES:
            raise ValueError('Неподдержанный тип публичного ресурса')
    return path


def attributes(name):
    path = valid_path(name)
    content_type = 'application/zip' if name == 'api.zip' else 'application/json' if name == 'gateway.json' else TYPES[path.suffix]
    cache = 'public, max-age=31536000, immutable' if name.startswith('web/assets/') else 'no-cache'
    return content_type, cache


def payloads(root):
    root = Path(root)
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise ValueError('Ожидается обычный каталог артефактов')
    result, folded, total = {}, set(), 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Символические ссылки не допускаются в наборе выпуска')
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        valid_path(name)
        if name.lower() in folded:
            raise ValueError('Пути артефактов отличаются только регистром')
        folded.add(name.lower())
        with path.open('rb') as source:
            data = source.read(MAX_OBJECT + 1)
        total += len(data)
        if not data or len(data) > MAX_OBJECT or total > MAX_BUNDLE:
            raise ValueError('Нарушен размер файла/набора выпуска')
        result[name] = data
    required = {'api.zip', 'gateway.json', 'web/index.html', 'web/service-worker.js', 'web/manifest.webmanifest'}
    if not required <= result.keys():
        raise ValueError('Набор API/Gateway/PWA неполон')
    return result


def validate_payloads(files):
    try:
        with zipfile.ZipFile(io.BytesIO(files['api.zip'])) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or 'index.py' not in names or 'requirements.txt' in names:
                raise ValueError('Некорректный готовый ZIP функции')
            if sum(item.file_size for item in archive.infolist()) > 680 * 1024 * 1024:
                raise ValueError('Распакованный ZIP превышает предел Cloud Functions')
            for item in archive.infolist():
                path = PurePosixPath(item.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in item.filename or ':' in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError('Небезопасный путь в ZIP функции')
                if item.flag_bits & 1:
                    raise ValueError('Зашифрованный ZIP не поддерживается')
                if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ValueError('Неподдержанное сжатие ZIP функции')
            if archive.testzip() is not None:
                raise ValueError('CRC файла внутри ZIP не совпадает')
    except zipfile.BadZipFile as error:
        raise ValueError('Некорректный ZIP функции') from error
    gateway = parse_request(files['gateway.json'])
    if not isinstance(gateway, dict) or gateway.get('openapi') not in {'3.0.0', '3.0.1', '3.1.0'} or not isinstance(gateway.get('paths'), dict):
        raise ValueError('Некорректный шаблон Gateway')
    pwa = parse_request(files['web/manifest.webmanifest'])
    if not isinstance(pwa, dict) or pwa.get('start_url') != '/' or pwa.get('scope') != '/':
        raise ValueError('Ожидается PWA с относительным корнем текущего origin')


def check_manifest(manifest):
    canonical_bytes(manifest)
    Draft202012Validator(SCHEMA).validate(manifest)
    expected_id = f"{manifest['main_sha']}_{manifest['build']['run_id']}_{manifest['build']['attempt']}"
    if manifest['release_id'] != expected_id:
        raise ValueError('release_id не соответствует SHA/run/attempt')
    names = [item['path'] for item in manifest['artifacts']]
    if len(names) != len(set(n.lower() for n in names)):
        raise ValueError('Повторяющиеся пути артефактов')
    if names != sorted(names):
        raise ValueError('Упорядочь артефакты по пути')
    for item in manifest['artifacts']:
        content_type, cache = attributes(item['path'])
        if (item['content_type'], item['cache_control']) != (content_type, cache):
            raise ValueError('Метаданные ресурса не соответствуют его пути')
    if not {'api.zip', 'gateway.json', 'web/index.html', 'web/service-worker.js', 'web/manifest.webmanifest'} <= set(names):
        raise ValueError('В манифесте отсутствует обязательный артефакт')


def create_manifest(root, main_sha, run_id, attempt, migration_image, revision):
    files = payloads(root)
    validate_payloads(files)
    manifest = {
        'format': 1, 'release_id': f'{main_sha}_{run_id}_{attempt}', 'main_sha': main_sha,
        'build': {'repository': 'boris-shurygin/gym', 'ref': 'refs/heads/main',
                  'workflow': '.github/workflows/deploy-staging.yml', 'run_id': run_id, 'attempt': attempt},
        'api': {'runtime': 'python312', 'entrypoint': 'index.handler'},
        'migration': {'image': migration_image, 'revision': revision},
        'compatibility': {'api_major': 1, 'sync_schema_versions': [1]},
        'artifacts': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                       'content_type': attributes(name)[0], 'cache_control': attributes(name)[1]}
                      for name, data in sorted(files.items())],
    }
    check_manifest(manifest)
    return manifest


def verify_bundle(manifest, root):
    check_manifest(manifest)
    files = payloads(root)
    if set(files) != {item['path'] for item in manifest['artifacts']}:
        raise ValueError('Фактический список файлов отличается от манифеста')
    for item in manifest['artifacts']:
        data = files[item['path']]
        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Байты артефакта отличаются от манифеста')
    validate_payloads(files)
    return files


def publish(manifest, root, store):
    # Полный preflight до первой записи; затем используй эти же байты.
    files = verify_bundle(manifest, root)
    published = set()
    for item in manifest['artifacts']:
        digest = item['sha256']
        if digest not in published:
            store.put('blobs/sha256/' + digest, files[item['path']])
            published.add(digest)
    raw = canonical_bytes(manifest)
    result = store.put('releases/' + manifest['release_id'] + '/manifest.json', raw)
    return {'release_id': manifest['release_id'], 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'object_key': result['key'], 'state': result['state']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    create.add_argument('--root', required=True)
    create.add_argument('--output', required=True)
    create.add_argument('--main-sha', required=True)
    create.add_argument('--run-id', type=int, required=True)
    create.add_argument('--attempt', type=int, default=1)
    create.add_argument('--migration-image', required=True)
    create.add_argument('--revision', required=True)
    upload = sub.add_parser('publish')
    upload.add_argument('--root', required=True)
    upload.add_argument('--manifest', required=True)
    args = parser.parse_args()
    if args.command == 'create':
        output = Path(args.output).resolve()
        if output.is_relative_to(Path(args.root).resolve()):
            parser.error('Манифест должен находиться вне каталога файлов выпуска')
        manifest = create_manifest(args.root, args.main_sha, args.run_id, args.attempt, args.migration_image, args.revision)
        data = canonical_bytes(manifest)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('xb') as target:
            target.write(data)
        print(json.dumps({'release_id': manifest['release_id'], 'manifest_sha256': hashlib.sha256(data).hexdigest()}))
    else:
        manifest = parse_request(Path(args.manifest).read_bytes())
        store = ImmutableStore(os.environ['GYM_IAM_TOKEN'], DELIVERY_BUCKET)
        print(json.dumps(publish(manifest, args.root, store)))


if __name__ == '__main__':
    main()
