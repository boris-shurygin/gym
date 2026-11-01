import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from infra.diagnostics_config import configure_diagnostics


class DiagnosticsConfigTests(unittest.TestCase):
    def test_secret_is_staging_only_and_old_release_keeps_exact_configuration(self):
        config = {'enabled': True, 'project': 'folder__b1ghdeobqrevsim6jg9o',
                  'secret_id': 'a' * 20, 'secret_version_id': 'b' * 20}
        base = {'environment': {}, 'secrets': [{'key': 'client_secret'}]}
        with patch('infra.diagnostics_config.Path.read_text', return_value=json.dumps(config)):
            old = configure_diagnostics(copy.deepcopy(base), {'artifacts': []}, SimpleNamespace(NAME='staging'))
            self.assertEqual(old, base)
            manifest = {'artifacts': [{'path': 'web/diagnostics.json'}]}
            new = configure_diagnostics(copy.deepcopy(base), manifest, SimpleNamespace(NAME='staging'))
            self.assertEqual(len(new['secrets']), 2)
            self.assertEqual(new['secrets'][1]['environmentVariable'], 'GYM_MONIUM_API_KEY')
            prod = configure_diagnostics(copy.deepcopy(base), manifest, SimpleNamespace(NAME='production'))
            self.assertEqual(prod, base)
