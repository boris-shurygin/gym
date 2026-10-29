import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from infra.auth_config import configure_api
from infra import staging_target, production_target


class AuthConfigurationTests(unittest.TestCase):
    def test_secrets_are_bound_to_staging_and_exact_version(self):
        config = '{"enabled":true,"client_id":"' + 'a' * 32 + '","secret_id":"' + 'b' * 20 + '","secret_version_id":"' + 'c' * 20 + '"}'
        source = {'environment': {}}
        manifest = {'migration': {'revision': '0002_accounts_programs'}}
        with patch.object(Path, 'read_text', return_value=config):
            payload = configure_api(copy.deepcopy(source), manifest, staging_target)
            self.assertEqual(payload['environment']['GYM_ORIGIN'], staging_target.ORIGIN)
            self.assertNotIn('GYM_YANDEX_CLIENT_SECRET', payload['environment'])
            self.assertEqual(payload['secrets'][0]['environmentVariable'], 'GYM_YANDEX_CLIENT_SECRET')
            self.assertEqual(payload['secrets'][0]['versionId'], 'c' * 20)
            evolved = {'migration': {'revision': '0003_workouts_sync'}}
            self.assertEqual(configure_api(copy.deepcopy(source), evolved, staging_target), payload)
            self.assertEqual(configure_api(copy.deepcopy(source), evolved, production_target), source)
            self.assertEqual(configure_api(copy.deepcopy(source), manifest, production_target), source)
            self.assertEqual(configure_api(copy.deepcopy(source), {'migration': {'revision': '0001_runtime_metadata'}}, staging_target), source)

    def test_unconfigured_disabled_auth_has_no_secret_or_client_id(self):
        with patch.object(Path, 'read_text', return_value='{"enabled":false,"client_id":null,"secret_id":null,"secret_version_id":null}'):
            self.assertEqual(configure_api({'environment': {}}, {'migration': {'revision': '0002_accounts_programs'}}, staging_target), {'environment': {}})
