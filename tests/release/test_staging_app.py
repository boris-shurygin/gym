import unittest
import copy
import json
from unittest.mock import Mock, patch

from infra import staging_app_access as access, staging_target as target
from infra import staging_app as app
from infra.staging_migrations import InterventionRequired
from contracts.canonical import canonical_bytes
from infra.release_artifacts import attributes


RELEASE = 'a' * 40 + '_123_1'


def fixture():
    files = {'gateway.json': canonical_bytes(app.TEMPLATE), 'api.zip': b'fixture',
             'web/index.html': b'<html>fixture</html>', 'web/service-worker.js': b'/* fixture */',
             'web/manifest.webmanifest': b'{"scope":"/","start_url":"/"}', 'web/icon.svg': b'<svg/>',
             'web/assets/index-hash.js': b'fixture'}
    manifest = {'release_id': RELEASE, 'main_sha': 'a' * 40, 'migration': {'revision': '0001_runtime_metadata'}, 'artifacts': [
        {'path': path, 'sha256': 'b' * 64, 'bytes': len(data), 'content_type': attributes(path)[0],
         'cache_control': attributes(path)[1]} for path, data in sorted(files.items())]}
    return manifest, files


class GatewayTests(unittest.TestCase):
    def test_update_bridge_bypasses_legacy_worker_without_exposing_auth_data(self):
        manifest, files = fixture()
        manifest['artifacts'].append({'path': 'web/update.html'})
        spec = app.render_gateway(manifest, files['gateway.json'])
        bridge = spec['paths']['/auth/update-app']
        self.assertEqual(set(bridge), {'get'})
        integration = bridge['get']['x-yc-apigateway-integration']
        self.assertEqual(integration['type'], 'object_storage')
        self.assertEqual(integration['object'], 'releases/' + RELEASE + '/web/update.html')
        self.assertEqual(spec['paths']['/auth/{path+}']['x-yc-apigateway-any-method']['x-yc-apigateway-integration']['type'], 'cloud_functions')

    def test_service_routes_are_never_static_and_api_uses_unique_tag(self):
        manifest, files = fixture()
        spec = app.render_gateway(manifest, files['gateway.json'])
        for route in ('/api/{path+}', '/auth/{path+}', '/oauth/{path+}', '/.well-known/{path+}', '/mcp'):
            integration = spec['paths'][route]['x-yc-apigateway-any-method']['x-yc-apigateway-integration']
            self.assertEqual(integration['type'], 'cloud_functions')
            self.assertEqual(integration['tag'], app.version_tag(RELEASE))
            self.assertEqual(integration['service_account_id'], target.GATEWAY_ACCOUNT)
        self.assertEqual(spec['paths']['/']['get']['x-yc-apigateway-integration']['object'], 'releases/' + RELEASE + '/web/index.html')
        for item in spec['paths'].values():
            for parameter in item.get('parameters', []):
                self.assertEqual(parameter['style'], 'simple')
                self.assertFalse(parameter['explode'])

    def test_assets_keep_same_urls_across_releases_and_no_spa_fallback(self):
        manifest, files = fixture()
        first = app.render_gateway(manifest, files['gateway.json'])
        second = app.render_gateway(manifest | {'release_id': 'c' * 40 + '_456_1'}, files['gateway.json'])
        self.assertEqual(first['paths']['/assets/{file+}'], second['paths']['/assets/{file+}'])
        self.assertNotIn('/{path+}', first['paths'])
        self.assertEqual(app.web_key(RELEASE, 'web/assets/index-hash.js'), 'assets/index-hash.js')

    def test_unknown_template_and_validation_in_ci_rejected(self):
        manifest, files = fixture()
        with self.assertRaises(ValueError): app.render_gateway(manifest, b'{"openapi":"3.0.0"}')
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), self.assertRaises(ValueError):
            app.render_gateway(manifest, b'anything', validation=True)

    def test_api_config_is_fixed_and_mismatch_does_not_reuse_tag(self):
        manifest, _ = fixture()
        config = app.api_payload(manifest)
        version = {k: v for k, v in config.items() if k not in {'tag', 'package'}} | {'status': 'ACTIVE', 'tags': config['tag']}
        self.assertTrue(app.api_matches(version, config))
        for key, value in [('serviceAccountId', 'other'), ('environment', {}), ('secrets', [{'id': 'bad'}])]:
            with self.subTest(key=key): self.assertFalse(app.api_matches(version | {key: value}, config))
        self.assertEqual(config['package']['objectName'], 'blobs/sha256/' + 'b' * 64)

    def test_private_preflight_uses_raw_function_event_and_checks_nested_status(self):
        response = canonical_bytes({'statusCode': 200, 'body': '{"status":"ok"}'})
        with patch.object(app, 'raw_request', return_value=(200, {}, response)) as request:
            app.private_api_smoke('synthetic-token', 'r-test')
            self.assertIn('&integration=raw', request.call_args.args[0])
            self.assertEqual(request.call_args.kwargs['payload']['path'], '/api/v1/health')
        response = canonical_bytes({'statusCode': 400, 'body': '{}'})
        with patch.object(app, 'raw_request', return_value=(200, {}, response)), self.assertRaises(InterventionRequired):
            app.private_api_smoke('synthetic-token', 'r-test')

    def test_gateway_patch_uses_json_field_mask_without_resetting_other_fields(self):
        cloud = Mock()
        resource = {'folderId': target.FOLDER, 'name': 'gym-gateway', 'domain': target.ORIGIN.removeprefix('https://')}
        cloud.json.side_effect = [resource, {'openapiSpec': '{}'}, {'id': 'operation'}, {'openapiSpec': '{"paths":{}}'}]
        app.switch_gateway(cloud, {'paths': {}})
        changed = cloud.json.call_args_list[2]
        self.assertEqual(changed.kwargs['method'], 'PATCH')
        self.assertEqual(changed.args[1]['updateMask'], 'openapiSpec')
        self.assertEqual(set(changed.args[1]), {'updateMask', 'openapiSpec'})


class DeploymentOrderTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.files = fixture()
        self.events = []
        def action(name, value=None):
            def call(*args, **kwargs): self.events.append(name); return value
            return call
        patches = {
            'read_manifest': action('manifest', self.manifest), 'load_payload': action('bytes', self.files),
            'render_gateway': action('render', {}), 'AppCloud': action('cloud', Mock()),
            'verify_migrations': action('migrations'), 'function_version': action('api', 'version'),
            'private_api_smoke': action('private-smoke'), 'upload_web': action('web'),
            'switch_gateway': action('gateway'), 'smoke': action('smoke'),
        }
        self.mocks = {}
        for name, side_effect in patches.items():
            started = patch.object(app, name, side_effect=side_effect)
            self.mocks[name] = started.start()
            self.addCleanup(started.stop)
        store = Mock()
        store.put.side_effect = action('receipt')
        started = patch.object(app, 'ImmutableStore', return_value=store)
        started.start(); self.addCleanup(started.stop)
        retention = Mock()
        retention.prepare.side_effect = action('tags-prepare')
        retention.finish.side_effect = action('tags-finish')
        started = patch.object(app, 'TagRetention', return_value=retention)
        started.start(); self.addCleanup(started.stop)

    def test_receipt_is_last_after_all_preparation_and_smoke(self):
        result = app.execute('unused', RELEASE, 'd' * 64)
        self.assertEqual(self.events, ['manifest','bytes','render','cloud','migrations','tags-prepare','api','private-smoke','web','gateway','smoke','receipt','tags-finish'])
        self.assertEqual(result['smoke'], 'succeeded')

    def test_failed_migration_stops_all_application_changes(self):
        self.mocks['verify_migrations'].side_effect = InterventionRequired('failed')
        with self.assertRaises(InterventionRequired): app.execute('unused', RELEASE, 'd' * 64)
        self.assertNotIn('api', self.events)
        self.assertNotIn('tags-prepare', self.events)
        self.assertNotIn('gateway', self.events)
        self.assertNotIn('receipt', self.events)

    def test_failed_upload_keeps_gateway_and_receipt_untouched(self):
        self.mocks['upload_web'].side_effect = RuntimeError('upload failed')
        with self.assertRaises(RuntimeError): app.execute('unused', RELEASE, 'd' * 64)
        self.assertNotIn('gateway', self.events)
        self.assertNotIn('receipt', self.events)

    def test_failed_smoke_does_not_publish_success(self):
        self.mocks['smoke'].side_effect = InterventionRequired('smoke failed')
        with patch.object(app.time, 'sleep'), self.assertRaises(InterventionRequired):
            app.execute('unused', RELEASE, 'd' * 64)
        self.assertNotIn('receipt', self.events)
        self.assertNotIn('tags-finish', self.events)

    def test_validation_does_not_rotate_release_tags(self):
        with patch.dict(app.os.environ, {'GITHUB_ACTIONS': ''}):
            app.execute('unused', RELEASE, 'd' * 64, validation=True)
        self.assertNotIn('tags-prepare', self.events)
        self.assertNotIn('tags-finish', self.events)

    def test_missing_manifest_prevents_any_cloud_mutation(self):
        self.mocks['read_manifest'].side_effect = ValueError('hash mismatch')
        with self.assertRaises(ValueError): app.execute('unused', RELEASE, 'd' * 64)
        self.assertEqual(self.events, [])


class AppPolicyTests(unittest.TestCase):
    def test_gateway_only_reads_public_files_and_cannot_read_receipts(self):
        rules = [s for s in access.web_policy()['Statement'] if s['Principal'] == {'CanonicalUser': target.GATEWAY_ACCOUNT}]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]['Action'], 's3:GetObject')
        self.assertTrue(all('/assets/' in path or '/web/' in path for path in rules[0]['Resource']))
        self.assertFalse(any('deployments' in path for path in rules[0]['Resource']))

    def test_ci_creation_requires_star_and_no_folder_role_is_planned(self):
        rules = access.web_policy()['Statement']
        put = next(s for s in rules if s.get('Sid') == 'CiCreate')
        self.assertEqual(put['Condition'], {'StringEquals': {'s3:if-none-match': '*'}})
        self.assertTrue(all(group in [('serverless','function'), ('serverless','api-gateway'), ('iam','service-account')]
                            for group, _, _, _ in access.BINDINGS))
        self.assertEqual({resource for _,resource,_,_ in access.BINDINGS},
                         {target.FUNCTION,target.GATEWAY,target.API_ACCOUNT,target.GATEWAY_ACCOUNT})


if __name__ == '__main__':
    unittest.main()
