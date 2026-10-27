"""Собери готовый ZIP Python 3.12/Linux x86_64 без установки зависимостей в облаке."""

import argparse
import csv
import hashlib
import json
import io
from pathlib import Path
from pathlib import PurePosixPath
import platform
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_UNPACKED = 680 * 1024 * 1024


def normalize_records(files):
    # pip переписывает хеши CLI-обёрток с временным shebang. Обёртки не
    # поставляются, поэтому RECORD должен описывать только включённые файлы.
    for name in files:
        if name.endswith('.dist-info/RECORD'):
            rows = [row for row in csv.reader(io.StringIO(files[name].decode('utf-8')))
                    if row and row[0] in files]
            output = io.StringIO(newline='')
            csv.writer(output, lineterminator='\n').writerows(sorted(rows))
            files[name] = output.getvalue().encode('utf-8')


def write_archive(files, destination):
    unpacked = sum(len(value) for value in files.values())
    if unpacked > MAX_UNPACKED:
        raise ValueError('Превышен лимит распакованного пакета')
    if 'requirements.txt' in files or 'index.py' not in files:
        raise ValueError('Нет entrypoint либо присутствует установочный манифест')
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, value in sorted(files.items()):
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
                raise ValueError('Недопустимое имя файла пакета')
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    if destination.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Превышен лимит ZIP-пакета')
    return unpacked


def smoke(archive):
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'AMD64'} or sys.version_info[:2] != (3, 12):
        raise RuntimeError('Для smoke нужен Linux x86_64 с Python 3.12')
    with tempfile.TemporaryDirectory(prefix='gym-api-smoke-') as temporary:
        with zipfile.ZipFile(archive) as source:
            for name in source.namelist():
                if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name or ':' in name:
                    raise ValueError('Небезопасное имя файла в пакете')
            source.extractall(temporary)
        code = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from api.runtime_probe import handler
response = handler({})
assert response['statusCode'] == 200
for name in ['sqlalchemy', 'ydb', 'ydb_dbapi', 'ydb_sqlalchemy', 'grpc', 'greenlet', 'rpds', 'jsonschema']:
    module = sys.modules[name]
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
print(response['body'])
"""
        subprocess.run([sys.executable, '-I', '-c', code, temporary], check=True)


def build(output_directory, *, allow_cross_build=False, run_smoke=False):
    native = platform.system() == 'Linux' and platform.machine() in {'x86_64', 'AMD64'}
    if sys.version_info[:2] != (3, 12) or not native and not allow_cross_build:
        raise RuntimeError('Используй Python 3.12/Linux; cross-build разрешён только для локальной приёмки')
    output_directory = Path(output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gym-api-build-') as temporary:
        target = Path(temporary) / 'packages'
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                        '--index-url', 'https://pypi.org/simple', '--no-compile', '--only-binary=:all:',
                        '--platform', 'manylinux_2_28_x86_64', '--platform', 'manylinux2014_x86_64',
                        '--implementation', 'cp', '--python-version', '3.12', '--abi', 'cp312',
                        '--target', str(target), '-r', str(ROOT / 'api/requirements.txt')], check=True)
        files = {}
        for path in target.rglob('*'):
            relative = path.relative_to(target)
            if path.is_file() and not {'__pycache__', 'bin', 'Scripts'} & set(relative.parts) and path.suffix != '.pyc':
                files[relative.as_posix()] = path.read_bytes()
        normalize_records(files)
        source_digest = hashlib.sha256()
        for directory in ['api', 'contracts']:
            for path in sorted((ROOT / directory).rglob('*')):
                if path.is_file() and path.suffix in {'.py', '.json'} and '__pycache__' not in path.parts:
                    name = path.relative_to(ROOT).as_posix()
                    data = path.read_bytes().replace(b'\r\n', b'\n') if path.suffix == '.py' else path.read_bytes()
                    files[name] = data
                    source_digest.update(name.encode() + b'\0' + data + b'\0')
        files['index.py'] = b'from api.handler import handler\n'
        candidate = Path(temporary) / 'api.zip'
        unpacked = write_archive(files, candidate)
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        destination = output_directory / f'api-{digest}.zip'
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError('Уже существует артефакт с несовпадающим содержимым')
        if not destination.exists():
            destination.write_bytes(candidate.read_bytes())
        manifest = {'format': 1, 'runtime': 'python312', 'entrypoint': 'index.handler',
                    'platform': 'linux-x86_64', 'build_mode': 'linux' if native else 'cross-wheel-validation',
                    'sha256': digest, 'bytes': destination.stat().st_size, 'unpacked_bytes': unpacked,
                    'source_tree_sha256': source_digest.hexdigest(),
                    'builder_sha256': hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest(),
                    'requirements_sha256': {name: hashlib.sha256((ROOT / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
                        for name in ['api/requirements.txt', 'api/runtime-requirements.txt', 'contracts/requirements.txt']}}
        manifest_path = output_directory / f'api-{digest}.json'
        metadata = json.dumps(manifest, sort_keys=True, indent=2) + '\n'
        if manifest_path.exists() and manifest_path.read_text(encoding='utf-8') != metadata:
            raise ValueError('Для другой сборки/проверки используй отдельный output-directory')
        if not manifest_path.exists():
            manifest_path.write_text(metadata, encoding='utf-8')
        if run_smoke:
            smoke(destination)
        print(json.dumps({'archive': str(destination), **manifest}, sort_keys=True))
        return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', default=str(ROOT / '.local/artifacts'))
    parser.add_argument('--allow-cross-build', action='store_true')
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    build(args.output_directory, allow_cross_build=args.allow_cross_build, run_smoke=args.smoke)


if __name__ == '__main__':
    main()
