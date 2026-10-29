import copy
import hashlib
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError

from contracts.canonical import canonical_bytes,parse_request
from infra import production_release as release, production_target as p, staging_target as s
from infra import staging_app as app, staging_migrations as m, production_access as access
from infra.staging_app_access import web_policy
from infra.delivery_access import policy as delivery_policy


NEW='a'*40+'_123_1'
OLD='b'*40+'_456_1'
DIGEST='c'*64


def manifest(identifier):
    return {'release_id':identifier,'main_sha':identifier[:40], 'build':{'run_id':int(identifier.split('_')[1])},
        'migration':{'revision':'0001_runtime_metadata'},'compatibility':{'api_major':1,'sync_schema_versions':[1]},
        'artifacts':[{'path':'web/index.html','sha256':DIGEST,'bytes':1}]}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.objects={}
        self.store=Mock()
        self.store.put.side_effect=lambda key,data,**kwargs:self.objects.setdefault(key,parse_request(data))
        patches={
            'installed':Mock(return_value=(OLD,hashlib.sha256(canonical_bytes({})).hexdigest())),
            'worker_state':Mock(return_value=(manifest(OLD),'succeeded')),
            'read_object':Mock(side_effect=lambda store,key:self.objects.get(key)),
        }
        self.mocks={}
        for name,value in patches.items():
            handle=patch.object(release,name,value);handle.start();self.addCleanup(handle.stop);self.mocks[name]=value
        for obj,name,value in [
            (release,'ImmutableStore',Mock(return_value=self.store)),
            (release.gate,'staged_release',Mock(side_effect=lambda token,identifier:(manifest(identifier),{'manifest_sha256':DIGEST}))),
            (app,'AppCloud',Mock()),(app,'render_gateway',Mock(return_value={})),
            (app,'execute',Mock(return_value={'smoke':'succeeded'})),(m,'execute',Mock(return_value={'state':'succeeded'})),
        ]:
            handle=patch.object(obj,name,value);handle.start();self.addCleanup(handle.stop);self.mocks[name]=value
        self.old_receipt={'environment':'production','release_id':OLD,'manifest_sha256':DIGEST,
            'purpose':'production-release','smoke':'succeeded'}

    def test_promote_runs_migrations_and_uses_fixed_production_target(self):
        result=release.execute('unused',NEW,'promote','999_1')
        self.assertEqual(result['previous_release_id'],OLD)
        m.execute.assert_called_once()
        self.assertIn('deployments/operations/999_1/intent.json',self.objects)
        self.assertIn('deployments/operations/999_1/result.json',self.objects)
        self.assertEqual(app.execute.call_args.kwargs['target'],p)
        self.assertEqual(m.execute.call_args.kwargs['target'],p)

    def test_failed_migration_keeps_application_untouched_and_intent_available(self):
        m.execute.side_effect=m.InterventionRequired('partial')
        with self.assertRaises(m.InterventionRequired): release.execute('unused',NEW,'promote','999_1')
        app.execute.assert_not_called()
        self.assertEqual(self.objects['deployments/operations/999_1/intent.json']['previous_release_id'],OLD)
        self.assertNotIn('deployments/operations/999_1/result.json',self.objects)

    def test_failed_application_does_not_publish_operation_success(self):
        app.execute.side_effect=m.InterventionRequired('smoke failed')
        with self.assertRaises(m.InterventionRequired): release.execute('unused',NEW,'promote','999_1')
        self.assertNotIn('deployments/operations/999_1/result.json',self.objects)

    def test_rollback_never_runs_migrations_and_keeps_current_schema_manifest(self):
        self.objects['deployments/'+OLD+'/result.json']=self.old_receipt
        self.mocks['installed'].return_value=(NEW,hashlib.sha256(canonical_bytes({})).hexdigest())
        self.mocks['worker_state'].return_value=(manifest(NEW),'succeeded')
        result=release.execute('unused',OLD,'rollback','999_1')
        m.execute.assert_not_called()
        self.assertEqual(app.execute.call_args.kwargs['schema_manifest']['release_id'],NEW)
        self.assertEqual(result['schema_release_id'],NEW)

    def test_rollback_requires_prior_success_in_this_environment(self):
        with self.assertRaises(ValueError): release.execute('unused',OLD,'rollback','999_1')
        app.execute.assert_not_called();m.execute.assert_not_called()

    def test_other_partial_job_blocks_new_release_before_any_write(self):
        self.mocks['worker_state'].return_value=(manifest(OLD),'needs_resume')
        with self.assertRaises(m.InterventionRequired): release.execute('unused',NEW,'promote','999_1')
        self.assertEqual(self.objects,{})
        m.execute.assert_not_called()

    def test_same_partial_release_can_resume(self):
        self.mocks['worker_state'].return_value=(manifest(NEW),'needs_resume')
        release.execute('unused',NEW,'promote','999_1')
        m.execute.assert_called_once()

    def test_repeat_keeps_original_previous_release_in_intent(self):
        first=release.execute('unused',NEW,'promote','999_1')
        self.mocks['installed'].return_value=(NEW,hashlib.sha256(canonical_bytes({})).hexdigest())
        self.mocks['worker_state'].return_value=(manifest(NEW),'succeeded')
        second=release.execute('unused',NEW,'promote','999_1')
        self.assertEqual(first,second)


class EnvironmentTests(unittest.TestCase):
    def test_known_bootstrap_allows_only_yc_server_for_this_fixed_origin(self):
        resource={'folderId':p.FOLDER,'name':'gym-gateway','domain':p.ORIGIN.removeprefix('https://')}
        cloud=Mock()
        cloud.json.side_effect=[resource,{'openapiSpec':canonical_bytes(release.BOOTSTRAP|{'servers':[{'url':p.ORIGIN}]}).decode()}]
        self.assertIsNone(release.installed(cloud,p)[0])
        cloud.json.side_effect=[resource,{'openapiSpec':canonical_bytes(release.BOOTSTRAP|{'servers':[{'url':'https://other.invalid'}]}).decode()}]
        with self.assertRaises(ValueError): release.installed(cloud,p)

    def test_same_package_maps_only_to_production_resources_and_relative_pwa_paths(self):
        value=manifest(NEW)
        value['artifacts'].append({'path':'api.zip','sha256':DIGEST,'bytes':1})
        api=app.api_payload(value,target=p)
        spec=app.render_gateway(value,canonical_bytes(app.TEMPLATE),target=p)
        self.assertEqual(api['functionId'],p.FUNCTION)
        self.assertEqual(api['serviceAccountId'],p.API_ACCOUNT)
        self.assertEqual(api['environment']['GYM_ENVIRONMENT'],'production')
        self.assertEqual(api['package']['objectName'],'blobs/sha256/'+DIGEST)
        self.assertEqual(spec['servers'],[{'url':p.ORIGIN}])
        self.assertNotIn(s.FUNCTION,canonical_bytes(spec).decode())
        self.assertNotIn(s.WEB_BUCKET,canonical_bytes(spec).decode())
        worker=m.revision_payload(value|{'migration':{'image':'registry@sha256:'+DIGEST}},target=p)
        self.assertEqual(worker['containerId'],p.CONTAINER)
        self.assertEqual(worker['serviceAccountId'],p.MIGRATOR)

    def test_production_ci_reads_staging_receipts_only_and_cannot_write_delivery(self):
        rules=web_policy(production_reader=True)['Statement']
        own=[r for r in rules if r['Principal']=={'CanonicalUser':p.CI}]
        self.assertEqual(len(own),1)
        self.assertEqual(own[0]['Action'],'s3:GetObject')
        self.assertTrue(own[0]['Resource'].endswith('/deployments/*/result.json'))
        own=[r for r in delivery_policy(production_reader=True)['Statement'] if r['Principal']=={'CanonicalUser':p.CI}]
        self.assertEqual(len(own),1);self.assertEqual(own[0]['Action'],'s3:GetObject')
        self.assertFalse(any(group[0]=='resource-manager' for group,_,_,_ in access.BINDINGS))

    def test_readiness_retries_only_transient_reads_and_not_denials(self):
        request=Mock(side_effect=[HTTPError('synthetic',503,'temporary',{},None),{'state':'ready'}])
        with patch.object(m.time,'sleep'): self.assertEqual(m.read_ready(request),{'state':'ready'})
        self.assertEqual(request.call_count,2)
        denied=Mock(side_effect=HTTPError('synthetic',403,'denied',{},None))
        with self.assertRaises(HTTPError): m.read_ready(denied)
        self.assertEqual(denied.call_count,1)
        # Четыре исключения, затем остановка; ошибка чтения не превращается в шаг миграции.
        failed=Mock(side_effect=[HTTPError('synthetic',503,'temporary',{},None) for _ in range(4)])
        with patch.object(m.time,'sleep'),self.assertRaises(HTTPError): m.read_ready(failed)
        self.assertEqual(failed.call_count,4)


if __name__=='__main__': unittest.main()
