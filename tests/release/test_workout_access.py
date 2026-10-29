import unittest
from unittest.mock import Mock, patch

from infra import workout_access as access
from infra.ydb_access import load_config, RUNTIME


class WorkoutAccessTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.path = self.config['database'] + '/' + access.TABLE
        self.action = {'path': self.path, 'subject': self.config['accounts']['api'] + '@as', 'permissions': sorted(RUNTIME)}
        self.driver = Mock()
        child = Mock()
        child.name = access.TABLE
        child.is_table.return_value = True
        self.driver.scheme_client.list_directory.return_value.children = [child]

    def test_plan_and_apply_touch_only_single_table_and_repeat_is_empty(self):
        with patch.object(access, 'iam_plan', return_value=[]), \
                patch.object(access, 'acl_plan', return_value=[self.action]), \
                patch.object(access, 'apply_acl') as apply:
            self.assertEqual(access.plan(Mock(), self.driver)['acl'], [self.action])
            apply.assert_not_called()
            access.plan(Mock(), self.driver, apply=True)
            apply.assert_called_once_with(self.driver, self.action)
        with patch.object(access, 'iam_plan', return_value=[]), patch.object(access, 'acl_plan', return_value=[]):
            self.assertEqual(access.plan(Mock(), self.driver)['acl'], [])

    def test_missing_migration_or_other_rights_block_before_mutation(self):
        with patch.object(access, 'apply_acl') as apply, patch.object(access, 'iam_plan', return_value=[]), \
                patch.object(access, 'acl_plan', return_value=[]):
            self.driver.scheme_client.list_directory.return_value.children = []
            self.assertTrue(access.plan(Mock(), self.driver)['migration_required'])
            with self.assertRaises(ValueError):
                access.plan(Mock(), self.driver, apply=True)
            apply.assert_not_called()
        with patch.object(access, 'apply_acl') as apply, patch.object(access, 'iam_plan', return_value=[]), \
                patch.object(access, 'acl_plan', return_value=[self.action | {'path': self.config['database']}]):
            with self.assertRaises(ValueError):
                access.plan(Mock(), self.driver, apply=True)
            apply.assert_not_called()
