"""Собери и проверь Linux-образ без облачных прав; это не публикация выпуска."""

import hashlib
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'gym-migrations:validation'


def build(output, *, purpose='validation-only'):
    subprocess.run(['docker', 'build', '--platform', 'linux/amd64', '-f', 'infra/migrations.Dockerfile',
                    '-t', IMAGE, '.'], cwd=ROOT, check=True)
    metadata = json.loads(subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'python', IMAGE,
        '-c', 'import json; from db.migrate import checksum,REVISION; print(json.dumps({"checksum":checksum(),"revision":REVISION}))'], text=True))
    container = subprocess.check_output(['docker', 'run', '-d', '--rm', '-p', '127.0.0.1:8088:8080',
        '-e', 'GYM_YDB_ENDPOINT=grpcs://unreachable.invalid:2135', '-e', 'GYM_YDB_DATABASE=/unused',
        '-e', 'GYM_YDB_AUTH=metadata', '-e', 'GYM_RELEASE_ID=validation', IMAGE], text=True).strip()
    try:
        for attempt in range(40):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8088/health', timeout=2) as response:
                    health = json.load(response)
                break
            except (OSError, urllib.error.URLError):
                if attempt == 39:
                    raise
                time.sleep(0.25)
        if health['checksum'] != metadata['checksum'] or health['mode'] != 'worker':
            raise ValueError('Smoke образа вернул другие метаданные')
    finally:
        subprocess.run(['docker', 'stop', '-t', '3', container], check=True)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'image.tar'
    subprocess.run(['docker', 'save', '-o', str(archive), IMAGE], check=True)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    metadata.update(source_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                    archive_sha256=digest, purpose=purpose,
                    config_digest=subprocess.check_output(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}'], text=True).strip())
    (output / 'image.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(metadata))
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path, default=ROOT / '.local/migration-image')
    args = parser.parse_args()
    build(args.output_directory)


if __name__ == '__main__':
    main()
