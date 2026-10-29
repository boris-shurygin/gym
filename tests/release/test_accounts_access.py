import copy
from unittest.mock import Mock, patch
import unittest

from infra import accounts_access as access, staging_target as target


class AccountsAccessTests(unittest.TestCase):
    def setUp(self):
        self.resource = {'id': 's' * 20, 'folder_id': target.FOLDER,
            'name': 'gym-staging-yandex-id', 'status': 'ACTIVE',
            'labels': {'gym-project': 'gym', 'gym-environment': 'staging', 'gym-client-id': 'a' * 32},
            'current_version': {'id': 'v' * 20, 'status': 'ACTIVE', 'payload_entry_keys': ['client_secret']}}
        self.bindings = []
        self.yc = Mock()
        self.yc.run.side_effect = self.call
        self.configured = {'environment': {'GYM_YANDEX_CLIENT_ID': 'a' * 32},
            'secrets': [{'id': 's' * 20, 'versionId': 'v' * 20}]}

    def call(self, *args):
        if args[:3] == ('lockbox', 'secret', 'get'):
            return self.resource
        if args[:3] == ('iam', 'service-account', 'get'):
            return {'id': target.API_ACCOUNT, 'folder_id': target.FOLDER, 'name': 'gym-staging-api'}
        if args[:3] == ('lockbox', 'secret', 'list-access-bindings'):
            return self.bindings
        raise AssertionError('Неожиданный облачный вызов')

    def test_plan_never_reads_payload_and_grants_only_api_on_exact_secret(self):
        with patch.object(access, 'configure_api', return_value=self.configured):
            self.assertEqual(access.secret_plan(self.yc), [{'secret_id': 's' * 20,
                'account': target.API_ACCOUNT, 'role': 'lockbox.payloadViewer'}])
            self.bindings = [{'role_id': 'lockbox.payloadViewer',
                'subject': {'id': target.API_ACCOUNT, 'type': 'serviceAccount'}}]
            self.assertEqual(access.secret_plan(self.yc), [])

    def test_wrong_folder_version_client_and_broad_rights_block(self):
        original = copy.deepcopy(self.resource)
        with patch.object(access, 'configure_api', return_value=self.configured):
            for changed in ({'folder_id': 'production'}, {'current_version': {'id': 'other'}},
                            {'labels': original['labels'] | {'gym-client-id': 'b' * 32}}):
                self.resource = original | changed
                with self.assertRaises(ValueError):
                    access.secret_plan(self.yc)
            self.resource = original
            for subject, role in ((target.CI, 'lockbox.payloadViewer'), (target.API_ACCOUNT, 'editor')):
                self.bindings = [{'role_id': role, 'subject': {'id': subject, 'type': 'serviceAccount'}}]
                with self.assertRaises(ValueError):
                    access.secret_plan(self.yc)

    def test_missing_tables_block_before_any_binding_mutation(self):
        with patch.object(access, 'configure_api', return_value=self.configured), \
                patch.object(access, 'reconcile', side_effect=ValueError('Миграция не завершена')):
            with self.assertRaises(ValueError):
                access.plan(self.yc, Mock(), apply=True)
        self.assertFalse(any(call.args[2] == 'add-access-binding' for call in self.yc.run.call_args_list))
