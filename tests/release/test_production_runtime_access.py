import copy
import unittest
from unittest.mock import Mock, patch

from infra import production_runtime_access as access, production_target as target
from infra.ydb_access import RUNTIME, READ_ONLY
from infra.accounts_access import secret_plan


class RuntimeAccessTests(unittest.TestCase):
    def setUp(self):
        self.driver = Mock()
        self.driver.scheme_client.list_directory.return_value.children = []
        self.yc = Mock()
        self.yc.run.return_value = {'folder_id': target.FOLDER, 'name': 'gym'}

    def test_plan_includes_only_six_tables_and_readonly_admissions(self):
        with patch.object(access, 'acl_plan', return_value=[]):
            result = access.table_plan(self.driver)
        self.assertTrue(result['migration_required'])
        self.assertEqual(len(result['acl']), 6)
        for action in result['acl']:
            self.assertEqual(action['subject'], target.API_ACCOUNT + '@as')
            self.assertTrue(action['path'].startswith(target.YDB_PATH + '/gym_'))
            permissions = READ_ONLY if action['path'].endswith('/gym_admissions') else RUNTIME
            self.assertEqual(set(action['permissions']), permissions)

    def test_root_admin_other_account_or_excess_permissions_stop_plan(self):
        safe = {'path': target.YDB_PATH + '/gym_sessions', 'subject': target.API_ACCOUNT + '@as',
                'permissions': sorted(RUNTIME)}
        for change in [{'path': target.YDB_PATH}, {'path': target.YDB_PATH + '/gym_migration_jobs'},
                       {'subject': target.CI + '@as'}, {'permissions': ['ydb.access.grant']},
                       {'path': target.YDB_PATH + '/gym_admissions'}]:
            with self.subTest(change=change), patch.object(access, 'acl_plan', return_value=[safe | change]):
                with self.assertRaises(ValueError): access.table_plan(self.driver)

    def test_missing_migration_prevents_every_mutation(self):
        with patch.object(access, 'acl_plan', return_value=[]), patch.object(access, 'secret_plan', return_value=[]), \
                patch.object(access, 'apply_acl') as apply:
            with self.assertRaises(ValueError): access.plan(self.yc, self.driver, apply=True)
        apply.assert_not_called()
        self.assertEqual(self.yc.run.call_count, 1)

    def test_ci_and_gateway_are_checked_without_runtime_allowlist(self):
        configs = []
        def checked(driver, config):
            configs.append(copy.deepcopy(config))
            return []
        with patch.object(access, 'acl_plan', checked): access.table_plan(self.driver)
        self.assertEqual({c['accounts']['api'] for c in configs[1:]}, {target.CI, target.GATEWAY_ACCOUNT})
        for config in configs[1:]:
            self.assertEqual(config['runtime_tables'], [])
            self.assertEqual(config['runtime_readonly_tables'], [])

    def test_staging_secret_is_rejected_for_production_before_any_binding(self):
        yc = Mock()
        yc.run.return_value = {'folder_id': 'b1ghdeobqrevsim6jg9o', 'name': 'gym-staging-yandex-id'}
        configured = {'environment': {'GYM_YANDEX_CLIENT_ID': 'a' * 32},
                      'secrets': [{'id': 'b' * 20, 'versionId': 'c' * 20}]}
        with patch('infra.accounts_access.configure_api', return_value=configured):
            with self.assertRaises(ValueError): secret_plan(yc, target=target)
        self.assertEqual(yc.run.call_count, 1)
