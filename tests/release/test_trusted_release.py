import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from infra import delivery_access, publish_release, release_bundle, release_gate as gate


SHA = 'a' * 40


def context_fixture():
    env = {'GITHUB_REPOSITORY': gate.REPOSITORY, 'GITHUB_REPOSITORY_ID': str(gate.REPOSITORY_ID),
           'GITHUB_REPOSITORY_OWNER_ID': str(gate.OWNER_ID), 'GITHUB_REF': 'refs/heads/main',
           'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_WORKFLOW_REF': gate.REPOSITORY + '/' + gate.WORKFLOW + '@refs/heads/main',
           'GITHUB_SHA': SHA, 'GITHUB_WORKFLOW_SHA': SHA, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
    repo = {'id': gate.REPOSITORY_ID, 'owner': {'id': gate.OWNER_ID}}
    base = {'repository': repo, 'head_repository': repo, 'head_sha': SHA, 'head_branch': 'main'}
    source = base | {'id': 100, 'run_attempt': 1, 'workflow_id': gate.CHECKS_ID, 'event': 'push',
                     'status': 'completed', 'conclusion': 'success', 'path': '.github/workflows/ci.yml'}
    current = base | {'id': 123, 'run_attempt': 2, 'workflow_id': 200, 'event': 'workflow_run', 'path': gate.WORKFLOW}
    event = {'action': 'completed', 'repository': repo, 'workflow_run': copy.deepcopy(source)}
    jobs = [{'name': name, 'status': 'completed', 'conclusion': 'success'}
            for name in ['contracts', 'api', 'ydb', 'web', 'changes']]
    return [env, event, source, current, {'id': 200, 'path': gate.WORKFLOW, 'state': 'active'}, jobs, SHA]


class GateTests(unittest.TestCase):
    def test_successful_exact_main_push(self):
        result = gate.validate_context(*context_fixture())
        self.assertEqual(result['release_id'], SHA + '_123_2')

    def test_rejects_pr_fork_stale_sha_failed_or_other_workflow(self):
        for case in ['pr', 'fork', 'owner', 'sha', 'workflow-code', 'current-main', 'attempt', 'path', 'checks', 'job', 'event']:
            with self.subTest(case=case):
                args = context_fixture()
                if case == 'pr': args[2]['event'] = 'pull_request'
                elif case == 'fork': args[2]['head_repository'] = {'id': 17}
                elif case == 'owner': args[0]['GITHUB_REPOSITORY_OWNER_ID'] = '17'
                elif case == 'sha': args[0]['GITHUB_SHA'] = 'b' * 40
                elif case == 'workflow-code': args[0]['GITHUB_WORKFLOW_SHA'] = 'b' * 40
                elif case == 'current-main': args[6] = 'b' * 40
                elif case == 'attempt': args[1]['workflow_run']['run_attempt'] = 2
                elif case == 'path': args[3]['path'] = '.github/workflows/other.yml'
                elif case == 'checks': args[2]['workflow_id'] = 17
                elif case == 'job': args[5][1]['conclusion'] = 'failure'
                else: args[1]['action'] = 'requested'
                with self.assertRaises(ValueError):
                    gate.validate_context(*args)

    def test_oidc_requires_exact_repository_workflow_and_run(self):
        context = gate.validate_context(*context_fixture())
        claims = {'iss': 'https://token.actions.githubusercontent.com', 'aud': gate.AUDIENCE, 'sub': gate.SUBJECT,
                  'repository': gate.REPOSITORY, 'repository_id': str(gate.REPOSITORY_ID),
                  'repository_owner_id': str(gate.OWNER_ID), 'ref': 'refs/heads/main', 'event_name': 'workflow_run',
                  'workflow_ref': gate.REPOSITORY + '/' + gate.WORKFLOW + '@refs/heads/main',
                  'sha': SHA, 'workflow_sha': SHA, 'run_id': '123', 'run_attempt': '2'}
        gate.validate_publisher_claims(claims, context)
        for key in claims:
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.validate_publisher_claims(claims | {key: 'unexpected'}, context)

    def test_bad_oidc_endpoint_never_receives_runtime_token(self):
        for url in ['http://token.actions.githubusercontent.com', 'https://evil.invalid',
                    'https://x.actions.githubusercontent.com@evil.invalid']:
            with patch.dict('os.environ', {'ACTIONS_ID_TOKEN_REQUEST_URL': url}), patch.object(gate, 'request_json') as request:
                with self.assertRaises(ValueError): gate.publisher_token({})
                request.assert_not_called()


class BundleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        api = io.BytesIO()
        with zipfile.ZipFile(api, 'w') as archive: archive.writestr('index.py', 'handler = None')
        files = {'api.zip': api.getvalue(), 'gateway.json': b'{"openapi":"3.0.0","paths":{}}',
                 'web/index.html': b'<!doctype html>', 'web/service-worker.js': b'/* fixture */',
                 'web/manifest.webmanifest': b'{"scope":"/","start_url":"/"}'}
        for name, body in files.items():
            path = self.root / 'payload' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        config = b'{"architecture":"amd64","os":"linux"}'
        config_id = hashlib.sha256(config).hexdigest()
        (self.root / 'worker').mkdir()
        self.archive = self.root / 'worker/image.tar'
        with tarfile.open(self.archive, 'w') as archive:
            for name, data in {'config.json': config,
                    'manifest.json': json.dumps([{'Config': 'config.json', 'RepoTags': ['gym-migrations:validation'], 'Layers': []}]).encode()}.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.metadata = {'format': 1, 'purpose': 'release', 'source_sha': SHA,
                         'api': {'build_mode': 'linux', 'runtime': 'python312', 'entrypoint': 'index.handler',
                                 'sha256': hashlib.sha256(files['api.zip']).hexdigest()},
                         'worker': {'source_sha': SHA, 'purpose': 'release', 'archive_sha256': release_bundle.digest_file(self.archive),
                                    'config_digest': 'sha256:' + config_id, 'revision': '0001_runtime_metadata'},
                         'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in files.items()}}
        self.write_metadata()

    def write_metadata(self):
        (self.root / 'build.json').write_text(json.dumps(self.metadata), encoding='utf-8')

    def verify(self):
        return release_bundle.verify_bundle(self.root, purpose='release', source_sha=SHA)

    def test_complete_bundle(self):
        self.assertEqual(self.verify(), self.metadata)

    def test_validation_archive_wrong_source_native_build_or_hash_rejected(self):
        for section, key, bad in [('worker', 'purpose', 'validation-only'), ('worker', 'source_sha', 'b' * 40),
                                  ('api', 'build_mode', 'cross-wheel-validation'), ('api', 'sha256', '0' * 64),
                                  ('worker', 'archive_sha256', '0' * 64), ('worker', 'config_digest', 'sha256:' + '0' * 64)]:
            original = self.metadata[section][key]
            self.metadata[section][key] = bad
            self.write_metadata()
            with self.subTest(section=section, key=key), self.assertRaises(ValueError): self.verify()
            self.metadata[section][key] = original

    def test_extra_file_and_changed_payload_rejected(self):
        path = self.root / 'credentials.json'
        path.write_text('{}')
        with self.assertRaises(ValueError): self.verify()
        path.unlink()
        (self.root / 'payload/web/index.html').write_bytes(b'changed')
        with self.assertRaises(ValueError): self.verify()

    def test_archive_path_or_link_rejected_even_with_new_hash(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)]:
            with tarfile.open(self.archive, 'w') as archive:
                info = tarfile.TarInfo(name)
                info.type = kind
                archive.addfile(info)
            self.metadata['worker']['archive_sha256'] = release_bundle.digest_file(self.archive)
            self.write_metadata()
            with self.subTest(name=name), self.assertRaises(ValueError): self.verify()

    def test_invalid_bundle_stops_before_docker_or_storage(self):
        self.metadata['purpose'] = 'validation-only'
        self.write_metadata()
        with patch.object(publish_release, 'docker') as docker, patch.object(publish_release, 'ImmutableStore') as store:
            with self.assertRaises(ValueError):
                publish_release.publish_bundle(self.root, {'source_sha': SHA}, 'unused')
            docker.assert_not_called()
            store.assert_not_called()

    def test_registry_failure_never_publishes_manifest(self):
        with patch.object(publish_release, 'docker', side_effect=RuntimeError('push failed')), patch.object(publish_release, 'publish') as publish:
            with self.assertRaises(RuntimeError):
                publish_release.publish_bundle(self.root, {'source_sha': SHA}, 'unused')
            publish.assert_not_called()

    def test_success_publishes_digest_and_manifest_last_without_token_file(self):
        writes = []
        image = publish_release.REPOSITORY + '@sha256:' + 'b' * 64
        def run(args, *, env=None):
            if env:
                config = Path(env['DOCKER_CONFIG'], 'config.json').read_text()
                self.assertNotIn('synthetic-token', config)
                self.assertEqual(env['GYM_REGISTRY_IAM_TOKEN'], 'synthetic-token')
            if '{{.Id}}' in args: return self.metadata['worker']['config_digest']
            if '{{json .RepoDigests}}' in args: return json.dumps([image])
            return ''
        class Store:
            def __init__(self, *args): pass
            def put(self, key, data):
                writes.append((key, data))
                return {'key': key, 'state': 'created'}
        with patch.object(publish_release, 'docker', side_effect=run), patch.object(publish_release, 'ImmutableStore', Store):
            result = publish_release.publish_bundle(self.root,
                {'source_sha': SHA, 'release_id': SHA + '_123_2', 'run_id': 123, 'attempt': 2}, 'synthetic-token')
        self.assertEqual(result['image'], image)
        self.assertEqual(writes[-1][0], 'releases/' + SHA + '_123_2/manifest.json')
        self.assertEqual(json.loads(writes[-1][1])['migration']['image'], image)

    def test_credential_helper_only_serves_fixed_registry(self):
        helper = Path(__file__).resolve().parents[2] / 'infra/docker-credential-gym'
        import os
        env = os.environ | {'GYM_REGISTRY_IAM_TOKEN': 'synthetic-token'}
        for server, code in [('cr.yandex', 0), ('other.invalid', 1)]:
            result = subprocess.run([sys.executable, str(helper), 'get'], input=server + '\n', env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, code)
            if code: self.assertNotIn('synthetic-token', result.stdout + result.stderr)
            else: self.assertEqual(json.loads(result.stdout), {'Username': 'iam', 'Secret': 'synthetic-token'})

    def test_docker_error_does_not_expose_output(self):
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'SECRET', 'SECRET')):
            with self.assertRaisesRegex(RuntimeError, 'сырые stdout/stderr не выводятся') as error:
                publish_release.docker(['push', 'fixture'])
            self.assertNotIn('SECRET', str(error.exception))


class DeliveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.bucket_api = patch.object(delivery_access, 'bucket_api', return_value={}).start()
        self.addCleanup(patch.stopall)

    def fake_yc(self, *, existing_policy=None, roles=None):
        class Fake:
            def run(self, *args):
                if args == ('iam', 'whoami'): return delivery_access.OPERATOR
                if args[:3] == ('storage', 'bucket', 'get'):
                    return {'folder_id': delivery_access.FOLDER, 'resource_id': delivery_access.BUCKET_ID, 'anonymous_access_flags':
                            {'read': False, 'list': False, 'config_read': False}, 'policy': existing_policy}
                if args[:3] == ('container', 'repository', 'get'):
                    return {'id': delivery_access.REPOSITORY, 'name': delivery_access.REGISTRY + '/gym-migrations'}
                if 'list-access-bindings' in args:
                    return roles or [] if args[:2] == ('container', 'repository') else []
                raise AssertionError('Unexpected mutation: ' + str(args))
        return Fake()

    def test_plan_is_read_only_and_repeat_is_empty(self):
        first = delivery_access.plan(self.fake_yc())
        self.assertTrue(first['set_bucket_policy'])
        self.assertTrue(first['add_repository_binding'])
        self.assertTrue(first['add_bucket_binding'])
        self.bucket_api.return_value = {'accessBindings': [{'roleId': delivery_access.UPLOADER,
            'subject': {'id': delivery_access.PUBLISHER, 'type': 'serviceAccount'}}]}
        repeated = delivery_access.apply(self.fake_yc(existing_policy=delivery_access.policy(), roles=[delivery_access.desired_binding()]))
        self.assertFalse(repeated['set_bucket_policy'])
        self.assertFalse(repeated['add_repository_binding'])
        self.assertFalse(repeated['add_bucket_binding'])

    def test_bucket_binding_drift_rejected(self):
        self.bucket_api.return_value = {'accessBindings': [{'roleId': 'storage.admin',
            'subject': {'id': delivery_access.PUBLISHER, 'type': 'serviceAccount'}}]}
        with self.assertRaises(ValueError): delivery_access.plan(self.fake_yc())

    def test_bucket_iam_never_added_without_protecting_policy(self):
        yc = self.fake_yc()
        original = yc.run
        yc.run = lambda *args: None if args[:3] == ('storage', 'bucket', 'update') else original(*args)
        with self.assertRaisesRegex(ValueError, 'Политика/отсутствие обходных ACL'):
            delivery_access.apply(yc)
        self.assertTrue(all(call.args[1] == 'listAccessBindings' for call in self.bucket_api.call_args_list))

    def test_unknown_policy_stops_before_any_mutation(self):
        with self.assertRaisesRegex(ValueError, 'неизвестную политику'):
            delivery_access.apply(self.fake_yc(existing_policy={'Statement': []}))

    def test_only_conditional_create_on_delivery_prefixes(self):
        policy = delivery_access.policy()
        allows = [s for s in policy['Statement'] if s['Principal'] == {'CanonicalUser': delivery_access.PUBLISHER} and s['Effect'] == 'Allow']
        self.assertEqual({s['Action'] for s in allows}, {'s3:GetObject', 's3:PutObject'})
        put = next(s for s in allows if s['Action'] == 's3:PutObject')
        self.assertEqual(put['Condition'], {'StringEquals': {'s3:if-none-match': '*'}})
        self.assertTrue(all(delivery_access.BUCKET in resource for s in allows for resource in s['Resource']))

    def test_drift_and_public_binding_rejected(self):
        self.assertEqual(delivery_access.check_bindings([delivery_access.desired_binding()], {delivery_access.PUSHER}), {delivery_access.PUSHER})
        for binding in [{'role_id': 'admin', 'subject': {'id': delivery_access.PUBLISHER}},
                        {'role_id': 'viewer', 'subject': {'type': 'system', 'id': 'allUsers'}}]:
            with self.assertRaises(ValueError): delivery_access.check_bindings([binding], {delivery_access.PUSHER})


if __name__ == '__main__':
    unittest.main()
