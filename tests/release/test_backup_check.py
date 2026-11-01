from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import Mock

from infra.backup_check import backup_status, target_path


class BackupCheckTests(unittest.TestCase):
    def test_restore_target_cannot_be_root_existing_application_or_another_path(self):
        config = {'database': '/ru-central1/cloud/staging'}
        for name in ['', '/', '../gym_sessions', 'gym_sessions', 'gym_p11_restore_20261031_194500/other']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                target_path(config, name)
        self.assertEqual(target_path(config, 'gym_p11_restore_20261031_194500'),
            config['database'] + '/gym_p11_restore_20261031_194500')

    def test_schedule_alone_does_not_make_a_stale_backup_fresh(self):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        client = Mock()
        client.run.side_effect = [
            {'deletion_protection': True, 'backup_config': {'backup_settings': [{
                'backup_schedule': {'daily_backup_schedule': {}}, 'backup_time_to_live': '604800s'}]}},
            [{'database_id': 'staging', 'id': 'old', 'created_at': old, 'status': 'READY'},
             {'database_id': 'production', 'id': 'foreign', 'created_at': now, 'status': 'READY'},
             {'database_id': 'staging', 'id': 'shadow', 'created_at': now, 'status': 'READY', 'description': 'SYSTEM_SHADOW'}],
        ]
        result = backup_status(client, {'database_id': 'staging', 'folder_id': 'folder'})
        self.assertTrue(result['daily_7_days'])
        self.assertFalse(result['fresh'])
        self.assertEqual(result['backup_id'], 'old')
