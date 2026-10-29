import unittest
from unittest.mock import Mock,patch

from infra import production_access as access,production_target as p,staging_target as s,delivery_access as delivery
from infra.staging_app_access import web_policy


class FakeYc:
    def __init__(self):
        self.bad_policy=False
        self.bad_resource=False
        self.extra_database_ci=False
        self.calls=[]
    def token(self): return 'synthetic-token'
    def run(self,*args):
        self.calls.append(args)
        if args==('iam','whoami'): return delivery.OPERATOR
        service,kind,verb,*tail=args
        if 'add' in verb or verb=='update': raise AssertionError('Read-only plan attempted a mutation')
        if verb=='list-access-bindings':
            if service=='ydb' and self.extra_database_ci:
                return [{'role_id':'ydb.editor','subject':{'id':p.CI,'type':'serviceAccount'}}]
            return []
        if service=='resource-manager': return {'cloud_id':'b1ghmudqihbvpnscd2fq'}
        if service=='storage':
            name=tail[0]
            identifier,folder=next((i,f) for n,i,f,_,_ in access.BUCKETS if n==name)
            policy=(None if name==p.WEB_BUCKET else delivery.policy() if name==delivery.BUCKET else web_policy())
            if self.bad_policy: policy={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':'*'}]}
            return {'name':name,'resource_id':identifier,'folder_id':folder,'policy':policy,
                'anonymous_access_flags':{'read':False,'list':False,'config_read':False}}
        identifier=tail[1] if tail[0]=='--id' else tail[0]
        names={p.FUNCTION:'gym-api',p.GATEWAY:'gym-gateway',p.CONTAINER:'gym-migrations',
            p.API_ACCOUNT:'gym-production-api',p.GATEWAY_ACCOUNT:'gym-production-gateway',
            p.MIGRATOR:'gym-production-migrations',p.DATABASE:'gym',delivery.REGISTRY:'gym-releases'}
        return {'id':identifier,'name':'foreign' if self.bad_resource else names[identifier],
            'folder_id':delivery.FOLDER if identifier==delivery.REGISTRY else p.FOLDER}


class AccessPlanTests(unittest.TestCase):
    def setUp(self):
        self.yc=FakeYc()
        handle=patch.object(access,'bucket_api',return_value={'accessBindings':[]})
        self.bucket=handle.start();self.addCleanup(handle.stop)
        handle=patch.object(access,'connect',return_value=Mock(__enter__=Mock(return_value=Mock()),__exit__=Mock(return_value=False)))
        handle.start();self.addCleanup(handle.stop)
        handle=patch.object(access,'acl_plan',return_value=[])
        self.acl=handle.start();self.addCleanup(handle.stop)

    def test_plan_is_read_only_and_checks_acl_for_ci_and_gateway_too(self):
        result=access.plan(self.yc)
        self.assertEqual(len(result['bindings']),11)
        self.assertEqual(len(result['buckets']),3)
        checked={call.args[1]['accounts']['api'] for call in self.acl.call_args_list}
        self.assertEqual(checked,{p.API_ACCOUNT,p.CI,p.GATEWAY_ACCOUNT})
        self.assertTrue(all(call.args[2]=='listAccessBindings' for call in self.bucket.call_args_list))

    def test_unknown_policy_or_foreign_resource_never_applies(self):
        self.yc.bad_policy=True
        with self.assertRaises(ValueError): access.plan(self.yc)
        self.yc.bad_policy=False;self.yc.bad_resource=True
        with self.assertRaises(ValueError): access.plan(self.yc)

    def test_ci_direct_ydb_role_is_rejected_even_when_api_roles_are_missing(self):
        self.yc.extra_database_ci=True
        with self.assertRaises(ValueError): access.plan(self.yc)


if __name__=='__main__': unittest.main()
