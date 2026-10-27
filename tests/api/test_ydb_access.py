"""Проверь границы плана прав без токенов, облака и изменений IAM."""

import copy
import json
import os
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import ydb
from infra import ydb_access as access
from scripts.verify_ydb_access import denied
from scripts import verify_ydb_access as cloud_check


def entry(name='', permissions=(), owner='operator@as', directory=False):
    return SimpleNamespace(name=name, owner=owner,
        permissions=[SimpleNamespace(subject=s, permission_names=list(p)) for s, p in permissions],
        interrupt_permission_inheritance=False,
        is_table=lambda: not directory, is_directory=lambda: directory)


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.config = access.load_config()
        self.root = self.config['database']
        self.api = self.config['accounts']['api'] + '@as'
        self.migration = self.config['accounts']['migrations'] + '@as'
        self.scheme = Mock()
        self.driver = SimpleNamespace(scheme_client=self.scheme)
        self.objects = {self.root: entry(directory=True)}
        self.scheme.describe_path.side_effect = self.objects.__getitem__
        self.scheme.list_directory.side_effect = lambda path: SimpleNamespace(children=[
            value for key, value in self.objects.items() if key.startswith(path + '/')
            and '/' not in key[len(path) + 1:]])
        self.bindings = []
        self.inherited = []
        self.yc = Mock()
        self.yc.run.side_effect = self.yc_call

    def yc_call(self, *args):
        if args[:3] == ('ydb', 'database', 'get'):
            return {'folder_id': self.config['folder_id'], 'name': 'gym', 'labels': {'gym-project': 'gym'}}
        if args[:3] == ('resource-manager', 'folder', 'get'):
            return {'cloud_id': self.config['cloud_id']}
        if args[:3] == ('resource-manager', 'cloud', 'get'):
            return {'organization_id': 'org'}
        if args[:3] == ('iam', 'service-account', 'get'):
            key = next(k for k, v in self.config['accounts'].items() if v == args[-1])
            return {'folder_id': self.config['folder_id'], 'name': 'gym-staging-' + key}
        if args[:3] == ('ydb', 'database', 'list-access-bindings'):
            return self.bindings
        if args[2] == 'list-access-bindings':
            return self.inherited
        if args[2] == 'add-access-binding':
            self.bindings.append({'role_id': args[6], 'subject': {'id': args[8], 'type': 'serviceAccount'}})
            return None
        raise AssertionError(args)

    def table(self, name, permissions=(), owner='operator@as', directory=False):
        item = entry(name.rsplit('/', 1)[-1], permissions, owner, directory)
        self.objects[self.root + '/' + name] = item
        return item

    def test_plan_is_read_only_and_limits_role_and_scope(self):
        result = access.reconcile(self.yc, self.config, self.driver)
        self.assertEqual({a['role'] for a in result['iam']}, {'ydb.auditor'})
        self.assertEqual(result['acl'], [{'path': self.root, 'subject': self.migration,
                                        'permissions': sorted(access.MIGRATIONS)}])
        self.assertFalse(any(c.args[2] == 'add-access-binding' for c in self.yc.run.call_args_list))
        self.scheme.modify_permissions.assert_not_called()

    def test_apply_then_repeat_keeps_unrelated_bindings(self):
        unrelated = {'role_id': 'ydb.editor', 'subject': {'id': 'operator'}}
        self.bindings.append(unrelated)
        def grant(path, settings):
            self.assertEqual(path, self.root)
            self.objects[path].permissions.append(SimpleNamespace(subject=self.migration,
                                                                  permission_names=access.MIGRATIONS))
        self.scheme.modify_permissions.side_effect = grant
        access.reconcile(self.yc, self.config, self.driver, apply=True)
        result = access.reconcile(self.yc, self.config, self.driver, apply=True)
        self.assertEqual(result['iam'], [])
        self.assertEqual(result['acl'], [])
        self.assertIn(unrelated, self.bindings)
        self.scheme.modify_permissions.assert_called_once()

    def test_extra_root_rights_block_before_any_iam_mutation(self):
        self.objects[self.root].permissions = [SimpleNamespace(subject=self.api,
            permission_names=['ydb.granular.create_table'])]
        with self.assertRaises(ValueError):
            access.reconcile(self.yc, self.config, self.driver, apply=True)
        self.assertFalse(any(c.args[2] == 'add-access-binding' for c in self.yc.run.call_args_list))
        self.scheme.modify_permissions.assert_not_called()

    def test_api_owner_is_rejected_even_without_explicit_acl(self):
        self.table('gym_private', owner=self.api)
        with self.assertRaises(ValueError):
            access.acl_plan(self.driver, self.config)

    def test_migration_may_own_its_tables(self):
        self.table('gym_migration_lock', owner=self.migration)
        self.assertEqual(len(access.acl_plan(self.driver, self.config)), 1)

    def test_stale_runtime_grant_on_unlisted_table_is_rejected(self):
        self.table('gym_old', [(self.api, access.RUNTIME)])
        with self.assertRaises(ValueError):
            access.acl_plan(self.driver, self.config)

    def test_table_allowlist_grants_only_dml(self):
        self.config['runtime_tables'] = ['gym_programs']
        self.table('gym_programs')
        result = access.acl_plan(self.driver, self.config)
        self.assertEqual(result[-1], {'path': self.root + '/gym_programs', 'subject': self.api,
                                     'permissions': sorted(access.RUNTIME)})
        self.assertTrue(all('create' not in p and 'grant' not in p and 'alter' not in p
                            for p in result[-1]['permissions']))

    def test_missing_table_or_directory_in_allowlist_is_rejected(self):
        self.config['runtime_tables'] = ['gym_programs']
        with self.assertRaises(ValueError):
            access.acl_plan(self.driver, self.config)
        self.table('gym_programs', directory=True)
        with self.assertRaises(ValueError):
            access.acl_plan(self.driver, self.config)

    def test_nested_unlisted_grants_are_rejected(self):
        self.table('nested', directory=True)
        self.table('nested/private', [(self.api, access.RUNTIME)])
        with self.assertRaises(ValueError):
            access.acl_plan(self.driver, self.config)

    def test_inheritance_break_is_rejected(self):
        self.table('gym_private').interrupt_permission_inheritance = True
        with self.assertRaises(ValueError):
            access.acl_plan(self.driver, self.config)

    def test_broad_iam_database_and_inherited_roles_are_rejected(self):
        subject = {'id': self.config['accounts']['api'], 'type': 'serviceAccount'}
        for target in (self.bindings, self.inherited):
            with self.subTest(inherited=target is self.inherited):
                target.append({'role_id': 'ydb.editor', 'subject': subject})
                with self.assertRaises(ValueError):
                    access.iam_plan(self.yc, self.config)
                target.clear()

    def test_public_broad_iam_is_rejected(self):
        self.inherited.append({'role_id': 'editor', 'subject': {'id': 'allUsers', 'type': 'system'}})
        with self.assertRaises(ValueError):
            access.iam_plan(self.yc, self.config)

    def test_unowned_database_is_rejected(self):
        self.yc.run.side_effect = lambda *args: {'folder_id': self.config['folder_id'], 'name': 'gym', 'labels': {}}
        with self.assertRaises(ValueError):
            access.iam_plan(self.yc, self.config)

    def test_config_rejects_production_and_unsafe_allowlists(self):
        for field, value in [('environment', 'production'), ('database_id', 'other'),
                             ('runtime_tables', ['gym_migration_lock']),
                             ('runtime_tables', ['gym_x', 'gym_x']),
                             ('runtime_tables', ['gym_x`;DROP TABLE x']),
                             ('runtime_tables', ['../gym_x'])]:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                candidate = copy.deepcopy(self.config)
                candidate[field] = value
                path = Path(directory) / 'config.json'
                path.write_text(json.dumps(candidate), encoding='utf-8')
                with self.assertRaises(ValueError):
                    access.load_config(path)

    def test_denial_requires_permission_error(self):
        issue = SimpleNamespace(issue_code=2028, message='Failed to resolve table status: AccessDenied.', issues=[])
        nested = SimpleNamespace(issue_code=2019, message='Query invalidated', issues=[issue])
        for error in (ydb.Unauthorized('denied'), ydb.SchemeError('Access denied'),
                      ydb.ConnectionLost('status = StatusCode.PERMISSION_DENIED'),
                      ydb.Aborted('wrapped', issues=[nested])):
            self.assertTrue(denied(Mock(side_effect=error)))
        for error in (ydb.Unavailable('network'), ydb.SchemeError('syntax error'),
                      ydb.ConnectionLost('status = StatusCode.UNAVAILABLE'),
                      ydb.Aborted('transaction conflict'),
                      ydb.Aborted('AccessDenied without a structured issue')):
            with self.assertRaises(type(error)):
                denied(Mock(side_effect=error))
        with self.assertRaises(AssertionError):
            denied(lambda: None)

    def test_cloud_entrypoints_refuse_github_before_token_or_mutation(self):
        for module in (access, cloud_check):
            with self.subTest(module=module.__name__), patch.dict(os.environ, GITHUB_ACTIONS='true'), \
                    patch('sys.argv', ['command']), patch.object(module, 'Yc') as cli:
                with self.assertRaises(ValueError):
                    module.main()
                cli.assert_not_called()

    def test_verifier_session_failure_has_no_retry(self):
        driver = Mock()
        driver.table_client.session.return_value.create.side_effect = ydb.Unavailable('network')
        with self.assertRaises(ydb.Unavailable):
            cloud_check.execute(driver, 'SELECT 1')
        driver.table_client.session.return_value.create.assert_called_once()
        settings = driver.table_client.session.return_value.create.call_args.args[0]
        self.assertEqual(settings.timeout, 15)

    def test_negative_checks_do_not_reuse_disconnected_driver(self):
        with patch.object(cloud_check, 'connect') as connect, \
                patch.object(cloud_check, 'execute', side_effect=ydb.ConnectionLost(
                    'status = StatusCode.PERMISSION_DENIED')) as execute:
            for _ in range(3):
                self.assertEqual(denied(lambda: cloud_check.execute_as(self.config, 'test-token', 'SELECT 1')),
                                 'gRPC PERMISSION_DENIED')
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(execute.call_count, 3)

    def test_verifier_cleans_only_its_table_after_partial_failure(self):
        operator, migration, api = Mock(), Mock(), Mock()
        objects = {'unrelated'}
        dropped = []
        operator.scheme_client.list_directory.side_effect = lambda path: SimpleNamespace(
            children=[SimpleNamespace(name=name) for name in objects])

        def execute(driver, text, *, scheme=False):
            name = re.search(r'`([^`]+)`', text).group(1).rsplit('/', 1)[-1]
            if driver is migration and name.endswith('_app'):
                objects.add(name)
            elif driver is migration:
                raise ydb.Unavailable('failure after first DDL')
            elif driver is operator and text.startswith('DROP TABLE'):
                dropped.append(name)
                objects.remove(name)
            else:
                raise AssertionError('Unexpected operation')

        contexts = []
        for driver in (operator, migration, api):
            context = MagicMock()
            context.__enter__.return_value = driver
            contexts.append(context)
        with patch.object(cloud_check, 'connect', side_effect=contexts), \
                patch.object(cloud_check, 'reconcile', return_value={'iam': [], 'acl': []}), \
                patch.object(cloud_check, 'execute', side_effect=execute), \
                patch.object(cloud_check.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixed')), \
                patch.object(cloud_check.time, 'sleep'):
            with self.assertRaises(ydb.Unavailable):
                cloud_check.verify(Mock(), self.config)
        self.assertEqual(objects, {'unrelated'})
        self.assertEqual(dropped, ['gym_acl_fixed_app'])


if __name__ == '__main__':
    unittest.main()
