"""Проверяй хранилище на синтетических секретах без обращения к keyring."""
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from threading import Event
import types
import unittest
from unittest.mock import Mock, patch

from gym_agent import storage


class MissingCollection(Exception):
    pass


class LockedCollection(Exception):
    pass


class SecretServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.addCleanup(patch.stopall)
        patch.object(storage, 'ROOT', self.root).start()
        patch.object(storage.Vault, '_platform', return_value='linux').start()
        self.connection = Mock()
        self.session = types.SimpleNamespace(encrypted=True)
        self.items = []
        self.collection = Mock()
        self.collection.is_locked.return_value = False
        self.collection.search_items.side_effect = lambda attrs: [item for item in self.items
            if all(item.get_attributes()[name] == value for name, value in attrs.items())]
        self.collection.create_item.side_effect = self.create_item
        self.service = types.ModuleType('secretstorage')
        self.service.dbus_init = Mock(return_value=self.connection)
        self.service.Collection = Mock(return_value=self.collection)
        self.service.ItemNotFoundException = MissingCollection
        self.service.LockedException = LockedCollection
        self.service.create_collection = Mock(side_effect=AssertionError('must not create a collection'))
        self.service.get_default_collection = Mock(side_effect=AssertionError('can create a collection'))
        self.util = types.ModuleType('secretstorage.util')
        self.util.open_session = Mock(return_value=self.session)
        patch.dict(sys.modules, {'secretstorage': self.service, 'secretstorage.util': self.util}).start()

    def create_item(self, label, attributes, raw, replace=False, content_type=None):
        item = Mock()
        item.get_attributes.return_value = dict(attributes)
        item.is_locked.return_value = False
        item.get_secret.return_value = raw
        item.set_secret.side_effect = lambda value, **_: setattr(item.get_secret, 'return_value', value)
        self.items.append(item)
        return item

    def assert_code(self, code, operation):
        with self.assertRaises(storage.ClientError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_separate_environment_and_origin_without_local_tokens(self):
        staging = storage.Vault('staging', origin='https://staging.example')
        production = storage.Vault('production', origin='https://gym.example')
        other_origin = storage.Vault('production', origin='https://other.example')
        staging.save({'refresh_token': 'synthetic-staging-token'})
        production.save({'refresh_token': 'synthetic-production-token'})
        self.assertEqual(staging.read()['refresh_token'], 'synthetic-staging-token')
        self.assertEqual(production.read()['refresh_token'], 'synthetic-production-token')
        self.assertFalse(other_origin.exists())
        self.assertEqual(len(self.items), 2)
        self.assertEqual(production.attributes, {'application': 'gym-python', 'environment': 'production',
            'resource': 'https://gym.example/mcp', 'client_id': 'gym-python',
            'workspace': hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()})
        self.assertEqual(list(self.root.iterdir()), [])
        self.service.Collection.assert_called_with(self.connection, session=self.session)
        self.assertEqual(self.connection.close.call_count, 5)

    def test_checkouts_do_not_share_tokens_under_independent_refresh_locks(self):
        first = storage.Vault()
        first.save({'refresh_token': 'synthetic-first-checkout'})
        with patch.object(storage, 'ROOT', self.root / 'other-checkout'):
            second = storage.Vault()
        self.assertNotEqual(first.path.parent, second.path.parent)
        self.assertNotEqual(first.attributes['workspace'], second.attributes['workspace'])
        self.assertFalse(second.exists())
        second.save({'refresh_token': 'synthetic-second-checkout'})
        self.assertEqual(first.read()['refresh_token'], 'synthetic-first-checkout')
        self.assertEqual(second.read()['refresh_token'], 'synthetic-second-checkout')

    def test_save_updates_only_exact_item_without_reading_secret(self):
        vault = storage.Vault()
        vault.save({'refresh_token': 'synthetic-first-token'})
        vault.save({'refresh_token': 'synthetic-second-token'})
        self.assertEqual(len(self.items), 1)
        self.items[0].get_secret.assert_not_called()
        self.assertEqual(vault.read()['refresh_token'], 'synthetic-second-token')

    def test_backend_schema_metadata_preserves_read_and_update_without_duplicates(self):
        def create_with_schema(label, attributes, raw, **kwargs):
            return self.create_item(label, dict(attributes, **{'xdg:schema': 'org.freedesktop.Secret.Generic'}),
                raw, **kwargs)

        self.collection.create_item.side_effect = create_with_schema
        vault = storage.Vault()
        vault.save({'refresh_token': 'synthetic-first-token'})
        self.assertTrue(vault.exists())
        self.assertEqual(vault.read()['refresh_token'], 'synthetic-first-token')
        vault.save({'refresh_token': 'synthetic-second-token'})
        self.assertEqual(vault.read()['refresh_token'], 'synthetic-second-token')
        self.assertEqual(len(self.items), 1)
        self.collection.create_item.assert_called_once()
        self.items[0].set_secret.assert_called_once()

    def test_schema_metadata_does_not_bypass_ambiguous_item_protection(self):
        vault = storage.Vault()
        self.create_item('First', vault.attributes, b'{}')
        self.create_item('Second', dict(vault.attributes, **{'xdg:schema': 'org.freedesktop.Secret.Generic'}), b'{}')
        self.assert_code('credential_storage_ambiguous', vault.read)
        self.assert_code('credential_storage_ambiguous', lambda: vault.save({'token': 'synthetic'}))

    def test_status_and_availability_do_not_read_tokens(self):
        vault = storage.Vault()
        self.assertFalse(vault.exists())
        vault.save({'refresh_token': 'synthetic-token'})
        self.assertTrue(vault.exists())
        vault.check_available()
        self.items[0].get_secret.assert_not_called()
        self.collection.unlock.assert_not_called()
        self.service.create_collection.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_locked_collection_never_reads_writes_or_unlocks(self):
        self.collection.is_locked.return_value = True
        vault = storage.Vault()
        for operation in (vault.check_available, vault.exists, vault.read, lambda: vault.save({'token': 'synthetic'})):
            self.assert_code('secure_storage_locked', operation)
        self.collection.search_items.assert_not_called()
        self.collection.create_item.assert_not_called()
        self.collection.unlock.assert_not_called()
        self.assertEqual(self.connection.close.call_count, 4)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_locked_item_never_reads_or_replaces_secret(self):
        vault = storage.Vault()
        vault.save({'token': 'synthetic'})
        self.items[0].is_locked.return_value = True
        self.assert_code('secure_storage_locked', vault.read)
        self.assert_code('secure_storage_locked', lambda: vault.save({'token': 'other'}))
        self.items[0].get_secret.assert_not_called()
        self.items[0].set_secret.assert_not_called()
        self.items[0].unlock.assert_not_called()

    def test_unavailable_service_has_no_plaintext_fallback(self):
        self.service.dbus_init.side_effect = OSError('synthetic service unavailable')
        vault = storage.Vault()
        for operation in (vault.check_available, vault.exists, vault.read, lambda: vault.save({'token': 'synthetic'})):
            self.assert_code('secure_storage_unavailable', operation)
        self.assertEqual(list(self.root.iterdir()), [])
        self.connection.close.assert_not_called()

    def test_missing_dependency_fails_without_local_fallback(self):
        with patch.dict(sys.modules, {'secretstorage': None}):
            self.assert_code('secure_storage_dependency_missing', storage.Vault().check_available)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_collection_is_not_created(self):
        self.service.Collection.side_effect = MissingCollection()
        self.assert_code('secure_storage_collection_missing', storage.Vault().check_available)
        self.service.get_default_collection.assert_not_called()
        self.service.create_collection.assert_not_called()
        self.connection.close.assert_called_once()

    def test_plaintext_session_is_refused_before_any_secret_access(self):
        self.session.encrypted = False
        vault = storage.Vault()
        self.assert_code('secure_storage_encryption_required', lambda: vault.save({'token': 'synthetic'}))
        self.service.Collection.assert_not_called()
        self.connection.close.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failures_close_connection_and_hide_exception_text(self):
        self.collection.search_items.side_effect = OSError('synthetic confidential error')
        self.assert_code('secure_storage_unavailable', storage.Vault().read)
        self.connection.close.assert_called_once()

    def test_missing_invalid_or_ambiguous_credentials(self):
        vault = storage.Vault()
        self.assert_code('login_required', vault.read)
        self.create_item('Gym', vault.attributes, b'not JSON')
        self.assert_code('credential_storage_invalid', vault.read)
        self.items[0].get_secret.return_value = json.dumps([]).encode()
        self.assert_code('credential_storage_invalid', vault.read)
        self.create_item('Gym duplicate', vault.attributes, b'{}')
        self.assert_code('credential_storage_ambiguous', vault.read)

    def test_items_with_extra_attributes_are_not_reused(self):
        vault = storage.Vault()
        self.create_item('Other', dict(vault.attributes, extra='different client'), b'{}')
        self.assertFalse(vault.exists())
        vault.save({'token': 'synthetic'})
        self.assertEqual(len(self.items), 2)
        self.items[0].get_secret.assert_not_called()
        self.items[0].set_secret.assert_not_called()

    def test_environment_and_origin_are_explicitly_validated(self):
        self.assert_code('invalid_environment', lambda: storage.Vault('other'))
        self.assert_code('invalid_environment_origin', lambda: storage.Vault('production'))
        for origin in ('http://gym.example', 'https://u:p@gym.example', 'https://gym.example/path',
                'https://gym.example?query', 'https://gym.example#fragment'):
            self.assert_code('invalid_environment_origin', lambda: storage.Vault(origin=origin))


@unittest.skipUnless(sys.platform.startswith('linux'), 'POSIX lock and file permissions')
class PosixLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root_patch = patch.object(storage, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.vault = storage.Vault()

    def assert_insecure(self, vault):
        with self.assertRaises(storage.ClientError) as caught:
            with vault.locked():
                self.fail('unsafe lock acquired')
        self.assertEqual(caught.exception.code, 'insecure_credential_storage')

    def test_independent_file_descriptors_serialize_refresh(self):
        entered, acquired, trying = Event(), Event(), Event()

        def contender():
            entered.wait(timeout=2)
            trying.set()
            with storage.Vault().locked():
                acquired.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.vault.locked():
                task = pool.submit(contender)
                entered.set()
                self.assertTrue(trying.wait(timeout=2))
                self.assertFalse(acquired.wait(timeout=0.15))
            task.result(timeout=3)
        self.assertTrue(acquired.is_set())
        parent = self.vault.path.parent
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.vault.path.with_suffix('.lock').stat().st_mode), 0o600)
        self.assertEqual([path.name for path in parent.iterdir()], ['staging.lock'])
        self.assertEqual(self.vault.path.with_suffix('.lock').read_bytes(), b'')

    def test_environments_have_independent_locks(self):
        production = storage.Vault('production', origin='https://gym.example')
        with self.vault.locked(), production.locked():
            self.assertEqual({path.name for path in self.vault.path.parent.iterdir()},
                {'staging.lock', 'production.lock'})

    def test_lock_timeout_and_release_after_failure(self):
        with self.vault.locked():
            with patch.object(storage.time, 'monotonic', side_effect=[0, 11]):
                with self.assertRaises(storage.ClientError) as caught:
                    with storage.Vault().locked():
                        self.fail('contended lock acquired')
            self.assertEqual(caught.exception.code, 'another_agent_is_refreshing')
        with self.assertRaises(RuntimeError):
            with self.vault.locked():
                raise RuntimeError('synthetic failure')
        with self.vault.locked():
            pass

    def test_lock_symlink_does_not_touch_target(self):
        self.vault.path.parent.mkdir(parents=True, mode=0o700)
        target = self.root / 'unrelated'
        target.write_text('unchanged')
        self.vault.path.with_suffix('.lock').symlink_to(target)
        self.assert_insecure(self.vault)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_directory_symlink_does_not_create_files_in_target(self):
        target = self.root / 'unrelated'
        target.mkdir()
        (self.root / '.local').symlink_to(target, target_is_directory=True)
        self.assert_insecure(self.vault)
        self.assertEqual(list(target.iterdir()), [])

    def test_insecure_directory_is_refused_without_changing_permissions(self):
        self.vault.path.parent.mkdir(parents=True, mode=0o755)
        self.vault.path.parent.chmod(0o755)
        self.assert_insecure(self.vault)
        self.assertEqual(stat.S_IMODE(self.vault.path.parent.stat().st_mode), 0o755)
        self.assertEqual(list(self.vault.path.parent.iterdir()), [])

    def test_insecure_or_shared_file_is_refused_without_changing_it(self):
        self.vault.path.parent.mkdir(parents=True, mode=0o700)
        lock = self.vault.path.with_suffix('.lock')
        lock.write_text('unchanged')
        lock.chmod(0o644)
        self.assert_insecure(self.vault)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)
        self.assertEqual(lock.read_text(), 'unchanged')
        lock.chmod(0o600)
        os.link(lock, self.root / 'other-link')
        self.assert_insecure(self.vault)


if __name__ == '__main__':
    unittest.main()
