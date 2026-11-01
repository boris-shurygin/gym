from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from infra.pilot_health import backup_health, validate_monitor_claims
from infra.release_gate import REPOSITORY, REPOSITORY_ID, OWNER_ID, AUDIENCE


class PilotHealthTests(unittest.TestCase):
    def test_backup_signal_ignores_foreign_shadow_and_expired_retention(self):
        now = datetime.now(timezone.utc)
        def item(id, **values):
            return {'id': id, 'databaseId': 'etn63tldguvkj5kcpi3r', 'status': 'READY',
                'completedAt': (now - timedelta(hours=25)).isoformat(),
                'backupSettings': {'backupTimeToLive': '604800s'}, **values}
        data = {'backups': [item('stale'), item('foreign', databaseId='production'),
            item('shadow', description='SYSTEM_SHADOW'), item('short', backupSettings={'backupTimeToLive': '172800s'})]}
        self.assertFalse(backup_health(data, now=now)['backup_ready'])
        data['backups'].append(item('fresh', completedAt=(now - timedelta(hours=2)).isoformat()))
        result = backup_health(data, now=now)
        self.assertTrue(result['backup_ready'])
        self.assertEqual(result['backup_id'], 'fresh')
        self.assertFalse(result['rpo_checked_by_this_monitor'])
        with self.assertRaises(ValueError):
            backup_health(data | {'nextPageToken': 'more'}, now=now)

    def test_only_trusted_main_staging_monitor_can_exchange_oidc(self):
        config = json.loads((Path(__file__).parents[2] / 'infra/bootstrap.json').read_text(encoding='utf-8'))
        subject = next(a['subject'] for a in config['accounts'] if a['key'] == 'staging_ci')
        sha = 'a' * 40
        claims = {'iss': config['federation']['issuer'], 'aud': AUDIENCE, 'sub': subject,
            'repository': REPOSITORY, 'repository_id': str(REPOSITORY_ID), 'repository_owner_id': str(OWNER_ID),
            'ref': 'refs/heads/main', 'workflow_ref': REPOSITORY + '/.github/workflows/pilot-health.yml@refs/heads/main',
            'event_name': 'schedule', 'sha': sha, 'workflow_sha': sha}
        with patch.dict('os.environ', {'GITHUB_SHA': sha, 'GITHUB_WORKFLOW_SHA': sha}):
            validate_monitor_claims(claims)
            for name, value in [('ref', 'refs/heads/feature'), ('sub', subject.replace('staging', 'production')),
                ('event_name', 'pull_request'), ('workflow_ref', 'other'), ('repository_id', '0'), ('workflow_sha', 'b' * 40)]:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    validate_monitor_claims(claims | {name: value})
