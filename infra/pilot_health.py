"""Внешняя проверка staging без cookie, ключей и доступа GitHub к данным YDB."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler

from infra.staging_target import ORIGIN
from infra import staging_target as target


def validate_monitor_claims(claims):
    from infra.release_gate import REPOSITORY, REPOSITORY_ID, OWNER_ID, AUDIENCE
    config = json.loads((Path(__file__).parent / 'bootstrap.json').read_text(encoding='utf-8'))
    account = next(a for a in config['accounts'] if a['key'] == 'staging_ci')
    expected = {'iss': config['federation']['issuer'], 'aud': AUDIENCE, 'sub': account['subject'],
        'repository': REPOSITORY, 'ref': 'refs/heads/main',
        'workflow_ref': REPOSITORY + '/.github/workflows/pilot-health.yml@refs/heads/main'}
    if (any(claims.get(k) != v for k, v in expected.items())
            or str(claims.get('repository_id')) != str(REPOSITORY_ID)
            or str(claims.get('repository_owner_id')) != str(OWNER_ID)
            or claims.get('event_name') not in {'schedule', 'workflow_dispatch'}
            or claims.get('sha') != os.environ.get('GITHUB_SHA')
            or claims.get('workflow_sha') != os.environ.get('GITHUB_WORKFLOW_SHA')
            or not os.environ.get('GITHUB_SHA') or not os.environ.get('GITHUB_WORKFLOW_SHA')):
        raise ValueError('Untrusted staging monitor identity')


def backup_health(value, *, now=None):
    now = now or datetime.now(timezone.utc)
    # Только постоянная staging-база; временные базы и SYSTEM_SHADOW не заменяют штатную копию.
    backups = [b for b in value.get('backups', []) if b.get('databaseId') == 'etn63tldguvkj5kcpi3r'
        and b.get('description') != 'SYSTEM_SHADOW' and b.get('status') == 'READY'
        and b.get('backupSettings', {}).get('backupTimeToLive') == '604800s']
    if value.get('nextPageToken'):
        raise ValueError('Incomplete backup metadata list')
    latest = max(backups, key=lambda b: b.get('completedAt', '')) if backups else None
    completed = datetime.fromisoformat(latest['completedAt'].replace('Z', '+00:00')) if latest else None
    age = (now - completed).total_seconds() if completed else None
    # Возраст последней завершённой копии — эксплуатационный сигнал, не доказательство RPO.
    return {'environment': 'staging', 'backup_ready': age is not None and 0 <= age <= 86400,
        'last_success_age_seconds': round(age) if age is not None else None,
        'backup_id': latest['id'] if latest else None, 'freshness_basis': 'completed_at',
        'rpo_checked_by_this_monitor': False}


def check_backups(token):
    url = 'https://ydb.api.cloud.yandex.net/ydb/v1/backups?folderId=' + target.FOLDER + '&pageSize=1000'
    with build_opener(NoRedirect()).open(Request(url, headers={'Authorization': 'Bearer ' + token}), timeout=30) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Backup metadata response too large')
        return backup_health(json.loads(raw))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe():
    try:
        with build_opener(NoRedirect()).open(Request(ORIGIN + '/api/v1/health/ready',
                headers={'User-Agent': 'Gym-pilot-health'}), timeout=40) as response:
            return response.status == 200 and json.loads(response.read(1024)) == {'status': 'ok'}
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backups-only', action='store_true')
    parser.add_argument('--yc')
    args = parser.parse_args()
    if args.backups_only:
        if args.yc:
            if os.environ.get('GITHUB_ACTIONS'):
                raise ValueError('Operator CLI mode is not allowed in CI')
            from infra.ydb_access import Yc
            token = Yc(args.yc).token(target.CI)
        else:
            from infra.release_gate import exchange_token
            token = exchange_token(target.CI, validate_monitor_claims)
        result = check_backups(token)
        print(json.dumps(result))
        if not result['backup_ready']:
            raise SystemExit(1)
        return
    for attempt in range(3):
        if probe():
            print(json.dumps({'environment': 'staging', 'ready': True, 'attempt': attempt + 1}))
            return
        if attempt < 2:
            time.sleep(10)
    print(json.dumps({'environment': 'staging', 'ready': False, 'attempts': 3}))
    raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Не печатай токен, HTTP response body или полный exception.
        print(json.dumps({'monitor_failed': type(error).__name__}), flush=True)
        raise SystemExit(1) from None
