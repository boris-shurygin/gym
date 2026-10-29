import copy
import hashlib
import io
import unittest
from unittest.mock import patch

from contracts.canonical import canonical_bytes
from infra import production_gate as gate, release_gate as source, staging_app as app, staging_target as s


SHA='a'*40
RELEASE=SHA+'_123_1'


def run_fixture():
    repo={'id':source.REPOSITORY_ID,'owner':{'id':source.OWNER_ID}}
    run={'id':123,'run_attempt':1,'head_sha':SHA,'head_branch':'main','repository':repo,
        'head_repository':repo,'event':'workflow_run','path':source.WORKFLOW,'workflow_id':42,
        'status':'completed','conclusion':'success'}
    jobs={'jobs':[{'name':name,'status':'completed','conclusion':'success'} for name in ('gate','build','publish','migrations','deploy')]}
    return run,{'id':42},jobs


class StagingGateTests(unittest.TestCase):
    def check(self,values):
        with patch.object(gate,'github',side_effect=values):
            return gate.check_staging_run(RELEASE)

    def test_exact_successful_attempt_with_all_jobs(self):
        self.assertEqual(self.check(run_fixture()),SHA)

    def test_fork_pr_wrong_attempt_sha_path_or_incomplete_deploy_denied(self):
        for field,value in [('event','pull_request'),('run_attempt',2),('head_sha','b'*40),
                ('path','.github/workflows/other.yml'),('conclusion','failure'),('head_repository',{'id':17})]:
            run,workflow,jobs=run_fixture()
            run[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): self.check([run,workflow,jobs])
        run,workflow,jobs=run_fixture()
        jobs['jobs'][-1]['conclusion']='skipped'
        with self.assertRaises(ValueError): self.check([run,workflow,jobs])
        jobs['total_count']=101
        with self.assertRaises(ValueError): self.check([run,workflow,jobs])

    def test_bad_release_identity_rejected(self):
        for value in ('latest',SHA+'_0_1',SHA+'_123_0',SHA+'_123_10001',SHA+'_9999999999999999_1'):
            with self.subTest(value=value),self.assertRaises(ValueError): gate.identity(value)

    def test_operator_validation_or_wrong_gateway_receipt_never_promoted(self):
        manifest={'release_id':RELEASE,'main_sha':SHA,'artifacts':[{'path':'web/index.html'}]}
        spec=app.render_gateway(manifest,canonical_bytes(app.TEMPLATE))
        receipt={'format':1,'release_id':RELEASE,'main_sha':SHA,'environment':'staging',
            'purpose':'staging-release','smoke':'succeeded','origin':s.ORIGIN,'function_tag':app.version_tag(RELEASE),
            'manifest_sha256':'b'*64,'gateway_sha256':hashlib.sha256(canonical_bytes(spec)).hexdigest()}
        for change in ({'purpose':'operator-validation'},{'smoke':'failed'},{'gateway_sha256':'0'*64}):
            store=unittest.mock.Mock()
            store._open.return_value=io.BytesIO(canonical_bytes(receipt|change))
            with patch.object(gate,'check_staging_run',return_value=SHA),patch.object(gate,'ImmutableStore',return_value=store),patch.object(gate,'read_manifest',return_value=manifest),patch.object(gate,'load_payload',return_value={'gateway.json':canonical_bytes(app.TEMPLATE)}):
                with self.subTest(change=change),self.assertRaises(ValueError): gate.staged_release('unused',RELEASE)


class DispatchTests(unittest.TestCase):
    def fixture(self):
        run,_,_=run_fixture()
        workflow={'id':77,'path':gate.WORKFLOW,'state':'active'}
        run=run|{'event':'workflow_dispatch','path':gate.WORKFLOW,'workflow_id':77}
        env={'GITHUB_SHA':SHA,'GITHUB_WORKFLOW_SHA':SHA,'GITHUB_REPOSITORY':source.REPOSITORY,
            'GITHUB_REPOSITORY_ID':str(source.REPOSITORY_ID),'GITHUB_REPOSITORY_OWNER_ID':str(source.OWNER_ID),
            'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch',
            'GITHUB_WORKFLOW_REF':source.REPOSITORY+'/'+gate.WORKFLOW+'@refs/heads/main',
            'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}
        event={'repository':run['repository'],'inputs':{'release_id':'b'*40+'_456_1','operation':'promote'}}
        return [env,event,run,workflow,SHA]

    def test_current_main_can_promote_older_verified_candidate(self):
        context=gate.validate_dispatch(*self.fixture())
        self.assertEqual(context['source_sha'],SHA)
        self.assertEqual(context['release_id'],'b'*40+'_456_1')

    def test_pr_branch_stale_workflow_or_wrong_run_denied(self):
        for field,value in [('GITHUB_REF','refs/heads/feature'),('GITHUB_EVENT_NAME','pull_request'),
                ('GITHUB_WORKFLOW_SHA','c'*40),('GITHUB_RUN_ATTEMPT','2')]:
            args=self.fixture();args[0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): gate.validate_dispatch(*args)
        args=self.fixture();args[-1]='c'*40
        with self.assertRaises(ValueError): gate.validate_dispatch(*args)

    def test_oidc_binds_production_environment_workflow_and_current_attempt(self):
        context=gate.validate_dispatch(*self.fixture())
        claims={'iss':'https://token.actions.githubusercontent.com','aud':source.AUDIENCE,
            'sub':'repo:boris-shurygin@8566203/gym@1424201451:environment:production','environment':'production',
            'repository':source.REPOSITORY,'repository_id':str(source.REPOSITORY_ID),'repository_owner_id':str(source.OWNER_ID),
            'ref':'refs/heads/main','event_name':'workflow_dispatch',
            'workflow_ref':source.REPOSITORY+'/'+gate.WORKFLOW+'@refs/heads/main',
            'sha':SHA,'workflow_sha':SHA,'run_id':'123','run_attempt':'1'}
        gate.validate_claims(claims,context)
        for key in claims:
            with self.subTest(key=key),self.assertRaises(ValueError): gate.validate_claims(claims|{key:'wrong'},context)


if __name__=='__main__': unittest.main()
