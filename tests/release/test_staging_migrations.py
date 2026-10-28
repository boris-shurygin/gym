import hashlib
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from infra import staging_access as access, staging_target as target
from infra import staging_migrations as migration, release_gate as gate
from infra.delivery_access import policy
from infra.release_artifacts import attributes
from contracts.canonical import canonical_bytes


SHA = 'a' * 40
RELEASE = SHA + '_123_1'
DIGEST = 'b' * 64


def manifest_fixture():
    names = ['api.zip', 'gateway.json', 'web/index.html', 'web/manifest.webmanifest', 'web/service-worker.js']
    return {'format': 1, 'release_id': RELEASE, 'main_sha': SHA,
            'build': {'repository': gate.REPOSITORY, 'ref': 'refs/heads/main', 'workflow': gate.WORKFLOW, 'run_id': 123, 'attempt': 1},
            'api': {'runtime': 'python312', 'entrypoint': 'index.handler'},
            'migration': {'image': 'cr.yandex/crpe91o8ka9e03n3n90c/gym-migrations@sha256:' + DIGEST, 'revision': '0001_runtime_metadata'},
            'compatibility': {'api_major': 1, 'sync_schema_versions': [1]},
            'artifacts': [{'path': name, 'sha256': DIGEST, 'bytes': 1, 'content_type': attributes(name)[0],
                           'cache_control': attributes(name)[1]} for name in names]}


def status(state):
    return {'state': state, 'job_id': 'migration_' + RELEASE, 'release_id': RELEASE, 'checksum': DIGEST,
            'revision': '0001_runtime_metadata', 'generation': 2}


class WorkerCloud:
    def __init__(self, statuses, steps=(), mode='worker'):
        self.statuses, self.steps, self.mode = list(statuses), list(steps), mode
        self.calls = []

    def json(self, url, payload=None):
        if url == target.URL + '/health':
            return {'state': 'ready', 'release_id': RELEASE, 'checksum': DIGEST, 'mode': self.mode}
        assert url == target.URL + '/v1/migrations'
        self.calls.append(payload['operation'])
        assert payload['release_id'] == RELEASE and payload['checksum'] == DIGEST
        assert payload['job_id'] == 'migration_' + RELEASE
        result = (self.statuses if payload['operation'] == 'status' else self.steps).pop(0)
        if isinstance(result, Exception): raise result
        return result


class StepTests(unittest.TestCase):
    def test_bounded_steps_and_final_actual_schema_status(self):
        cloud = WorkerCloud([status('not_found'), status('succeeded')], [status('needs_resume'), status('succeeded')])
        result = migration.run_steps(cloud, manifest_fixture())
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(cloud.calls, ['status', 'step', 'step', 'status'])

    def test_repeat_completed_job_never_calls_step(self):
        cloud = WorkerCloud([status('succeeded'), status('succeeded')])
        migration.run_steps(cloud, manifest_fixture())
        self.assertEqual(cloud.calls, ['status', 'status'])

    def test_lost_response_reads_status_and_does_not_repeat_step(self):
        cloud = WorkerCloud([status('not_found'), status('succeeded'), status('succeeded')], [TimeoutError()])
        migration.run_steps(cloud, manifest_fixture())
        self.assertEqual(cloud.calls, ['status', 'step', 'status', 'status'])

    def test_uncertain_running_or_repair_state_stops_without_recovery(self):
        for state in ['running', 'repair_required', 'not_found', 'unexpected']:
            cloud = WorkerCloud([status('not_found'), status(state)], [TimeoutError()])
            with self.subTest(state=state), self.assertRaises(migration.InterventionRequired):
                migration.run_steps(cloud, manifest_fixture())
            self.assertEqual(cloud.calls.count('step'), 1)

    def test_conflict_is_not_retried(self):
        error = HTTPError(target.URL, 409, 'conflict', {}, io.BytesIO(b'{}'))
        cloud = WorkerCloud([status('not_found')], [error])
        with self.assertRaises(migration.InterventionRequired): migration.run_steps(cloud, manifest_fixture())
        self.assertEqual(cloud.calls, ['status', 'step'])

    def test_server_error_can_only_resume_from_durable_checkpoint(self):
        error = HTTPError(target.URL, 503, 'unavailable', {}, io.BytesIO(b'{}'))
        cloud = WorkerCloud([status('not_found'), status('needs_resume'), status('succeeded')], [error, status('succeeded')])
        migration.run_steps(cloud, manifest_fixture())
        self.assertEqual(cloud.calls, ['status', 'step', 'status', 'step', 'status'])

    def test_wrong_release_checksum_revision_or_final_success_rejected(self):
        for key in ['release_id', 'checksum', 'revision', 'job_id']:
            cloud = WorkerCloud([status('succeeded') | {key: 'other'}])
            with self.subTest(key=key), self.assertRaises(ValueError): migration.run_steps(cloud, manifest_fixture())
            self.assertNotIn('step', cloud.calls)
        cloud = WorkerCloud([status('succeeded'), status('running')])
        with self.assertRaises(migration.InterventionRequired): migration.run_steps(cloud, manifest_fixture())

    def test_bound_limits_steps_and_wrong_mode_stops_before_status(self):
        cloud = WorkerCloud([status('not_found')], [status('needs_resume')] * 8)
        with self.assertRaises(migration.InterventionRequired): migration.run_steps(cloud, manifest_fixture())
        self.assertEqual(cloud.calls.count('step'), 8)
        cloud = WorkerCloud([], mode='bootstrap')
        with self.assertRaises(ValueError): migration.run_steps(cloud, manifest_fixture())
        self.assertEqual(cloud.calls, [])


class ManifestAndDeployTests(unittest.TestCase):
    def test_all_artifact_hashes_verified_before_cloud_changes(self):
        raw = canonical_bytes(manifest_fixture())
        class Store:
            def _open(self, *args): return io.BytesIO(raw)
            def verify(self, *args): return False
        with patch.object(migration, 'ImmutableStore', return_value=Store()), patch.object(migration, 'Cloud') as cloud:
            with self.assertRaises(ValueError): migration.execute('unused', RELEASE, hashlib.sha256(raw).hexdigest())
            cloud.assert_not_called()

    def test_manifest_hash_and_release_mismatch_stop_before_artifact_reads(self):
        raw = canonical_bytes(manifest_fixture())
        class Store:
            def _open(self, *args): return io.BytesIO(raw)
            def verify(self, *args): raise AssertionError('No reads before manifest verification')
        with patch.object(migration, 'ImmutableStore', return_value=Store()):
            with self.assertRaises(ValueError): migration.read_manifest('unused', RELEASE, '0' * 64)
            with self.assertRaises(ValueError): migration.read_manifest('unused', SHA + '_124_1', hashlib.sha256(raw).hexdigest())

    def active(self):
        payload = migration.revision_payload(manifest_fixture())
        return {key: value for key, value in payload.items() if key not in {'imageSpec', 'description'}} | {
            'id': 'revision', 'status': 'ACTIVE', 'image': {'imageDigest': 'sha256:' + DIGEST,
            'environment': payload['imageSpec']['environment']}}

    def test_matching_revision_is_reused_without_mutation(self):
        current = self.active()
        with patch.object(migration, 'active_revision', return_value=current):
            from unittest.mock import Mock
            cloud = Mock()
            self.assertEqual(migration.deploy_worker(cloud, manifest_fixture()), 'revision')
            cloud.json.assert_not_called()

    def test_config_drift_and_command_override_are_not_accepted(self):
        expected = migration.revision_payload(manifest_fixture())
        current = self.active()
        self.assertTrue(migration.matches(current, expected))
        for key, bad in [('serviceAccountId', 'other'), ('secrets', [{'id': 'secret'}]), ('status', 'CREATING')]:
            with self.subTest(key=key): self.assertFalse(migration.matches(current | {key: bad}, expected))
        current['image']['command'] = {'command': ['untrusted']}
        self.assertFalse(migration.matches(current, expected))

    def test_failed_deploy_never_invokes_worker(self):
        from unittest.mock import Mock
        cloud = Mock()
        cloud.json.side_effect = TimeoutError()
        with patch.object(migration, 'active_revision', return_value={}), patch.object(migration, 'run_steps') as run:
            with self.assertRaises(TimeoutError): migration.deploy_worker(cloud, manifest_fixture())
            run.assert_not_called()
            self.assertEqual(cloud.json.call_count, 1)

    def test_bootstrap_in_ci_never_deploys(self):
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), patch.object(migration, 'read_manifest', return_value=manifest_fixture()), patch.object(migration, 'deploy_worker') as deploy:
            with self.assertRaises(ValueError): migration.execute('unused', RELEASE, DIGEST, bootstrap=True)
            deploy.assert_not_called()

    def test_operator_bootstrap_does_not_execute_application_migration(self):
        from unittest.mock import Mock
        cloud = Mock()
        cloud.json.return_value = {'state': 'ready', 'checksum': DIGEST, 'revision': '0001_runtime_metadata'}
        with patch.object(migration, 'read_manifest', return_value=manifest_fixture()), patch.object(migration, 'Cloud', return_value=cloud), patch.object(migration, 'deploy_worker', return_value='revision') as deploy, patch.object(migration, 'health', return_value={'checksum': DIGEST}), patch.object(migration, 'run_steps') as steps, patch.dict('os.environ', {}, clear=True):
            result = migration.execute('unused', RELEASE, DIGEST, bootstrap=True)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual([call.kwargs.get('mode', 'worker') for call in deploy.call_args_list], ['bootstrap', 'worker'])
        steps.assert_not_called()

    def test_staging_oidc_rejects_publisher_or_production_subject(self):
        context = {'source_sha': SHA, 'run_id': 123, 'attempt': 1}
        claims = {'iss': 'https://token.actions.githubusercontent.com', 'aud': gate.AUDIENCE,
                  'sub': 'repo:boris-shurygin@8566203/gym@1424201451:environment:staging', 'environment': 'staging',
                  'repository': gate.REPOSITORY, 'repository_id': str(gate.REPOSITORY_ID), 'repository_owner_id': str(gate.OWNER_ID),
                  'ref': 'refs/heads/main', 'event_name': 'workflow_run', 'workflow_ref': gate.REPOSITORY + '/' + gate.WORKFLOW + '@refs/heads/main',
                  'sha': SHA, 'workflow_sha': SHA, 'run_id': '123', 'run_attempt': '1'}
        gate.validate_publisher_claims(claims, context, staging=True)
        for key, value in [('sub', gate.SUBJECT), ('environment', 'production'), ('ref', 'refs/heads/feature'), ('workflow_sha', 'b' * 40)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.validate_publisher_claims(claims | {key: value}, context, staging=True)


class StagingAccessTests(unittest.TestCase):
    def test_reader_policy_keeps_publisher_protection_and_is_read_only(self):
        old = policy(staging_reader=False)
        new = policy()
        self.assertEqual(new['Statement'][:-1], old['Statement'])
        reader = new['Statement'][-1]
        self.assertEqual(reader['Principal'], {'CanonicalUser': target.CI})
        self.assertEqual(reader['Action'], 's3:GetObject')
        self.assertTrue(all('/blobs/sha256/' in path or '/releases/' in path for path in reader['Resource']))

    def test_unknown_roles_or_public_bindings_rejected(self):
        for binding in [{'role_id': 'admin', 'subject': {'id': target.CI}},
                        {'role_id': access.EDITOR, 'subject': {'type': 'system', 'id': 'allUsers'}}]:
            with self.assertRaises(ValueError): access.own_roles([binding], {access.EDITOR})
        self.assertEqual(access.own_roles([{'role_id': access.EDITOR, 'subject': {'id': target.CI}}], {access.EDITOR}), {access.EDITOR})

    def test_only_fixed_worker_or_bootstrap_configuration(self):
        env = target.worker_environment('release')
        self.assertEqual(env['GYM_YDB_DATABASE'], target.YDB_PATH)
        self.assertEqual(env['GYM_YDB_AUTH'], 'metadata')
        self.assertEqual(env['GYM_TABLE_PREFIX'], 'gym_')
        with self.assertRaises(ValueError): target.worker_environment('release', 'recovery')


if __name__ == '__main__':
    unittest.main()
