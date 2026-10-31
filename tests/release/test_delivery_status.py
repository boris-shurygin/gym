import unittest
from unittest.mock import patch
import io

from infra.delivery_status import state_for, verify_identity, get_json, CI, STAGING, ORIGIN

SHA = 'a' * 40


def run(path, identifier, conclusion='success', event='push'):
    return {'id': identifier, 'head_sha': SHA, 'head_branch': 'main', 'path': path,
        'event': event, 'status': 'completed', 'conclusion': conclusion,
        'created_at': '2026-10-31T10:00:00Z', 'updated_at': '2026-10-31T10:00:00Z', 'run_attempt': 1}


class DeliveryStatusTests(unittest.TestCase):
    def test_github_token_is_never_sent_to_the_application(self):
        with patch.dict('os.environ', {'GH_TOKEN': 'synthetic-token'}), patch('infra.delivery_status.build_opener') as factory:
            factory.return_value.open.return_value = io.BytesIO(b'{}')
            get_json(ORIGIN + '/__gym/release')
            request = factory.return_value.open.call_args.args[0]
            self.assertIsNone(request.get_header('Authorization'))

    def test_only_successful_exact_main_and_staging_can_be_ready(self):
        checks, stage = run(CI, 1), run(STAGING, 2, event='workflow_run')
        result = state_for([checks, stage], SHA)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['release_id'], SHA + '_2_1')
        for changed in (stage | {'conclusion': 'failure'}, stage | {'status': 'in_progress'},
                stage | {'head_sha': 'b' * 40}, stage | {'event': 'pull_request'}):
            self.assertNotEqual(state_for([checks, changed], SHA)['state'], 'ready')
        self.assertEqual(state_for([checks | {'conclusion': 'failure'}, stage], SHA)['state'], 'failed')

    def test_old_stage_does_not_satisfy_a_newer_check_attempt(self):
        checks = run(CI, 1) | {'updated_at': '2026-10-31T10:01:00Z'}
        self.assertEqual(state_for([checks, run(STAGING, 2, event='workflow_run')], SHA)['state'], 'waiting')

    def test_public_identity_must_match_and_is_not_a_replacement_for_ci(self):
        result = {'release_id': SHA + '_2_1'}
        value = {'main_sha': SHA, 'purpose': 'staging-release', **result}
        verify_identity(value, result, SHA)
        for key in value:
            with self.assertRaises(ValueError):
                verify_identity(value | {key: 'wrong'}, result, SHA)
