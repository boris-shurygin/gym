"""Покажи/примени фиксированный план IAM/ACL production только от имени оператора."""

import argparse
import json
import os
from pathlib import Path
import tempfile
from urllib.request import Request, build_opener

from infra import production_target as p, staging_target as s, delivery_access as delivery
from infra.staging_app_access import web_policy
from infra.migration_container import cloud
from infra.object_store import NoRedirect
from infra.ydb_access import Yc, connect, acl_plan, apply_acl
from infra.storage_operations import wait as wait_bucket


BINDINGS = [
    (('serverless','function'), p.FUNCTION, 'functions.editor', p.CI),
    (('serverless','function'), p.FUNCTION, 'functions.functionInvoker', p.CI),
    (('serverless','function'), p.FUNCTION, 'functions.functionInvoker', p.GATEWAY_ACCOUNT),
    (('serverless','api-gateway'), p.GATEWAY, 'api-gateway.editor', p.CI),
    (('serverless','container'), p.CONTAINER, 'serverless-containers.editor', p.CI),
    (('iam','service-account'), p.API_ACCOUNT, 'iam.serviceAccounts.user', p.CI),
    (('iam','service-account'), p.GATEWAY_ACCOUNT, 'iam.serviceAccounts.user', p.CI),
    (('iam','service-account'), p.MIGRATOR, 'iam.serviceAccounts.user', p.CI),
    (('container','registry'), delivery.REGISTRY, 'container-registry.images.puller', p.MIGRATOR),
    (('ydb','database'), p.DATABASE, 'ydb.auditor', p.API_ACCOUNT),
    (('ydb','database'), p.DATABASE, 'ydb.auditor', p.MIGRATOR),
]
BUCKETS = [
    (p.WEB_BUCKET, p.WEB_BUCKET_ID, p.FOLDER, web_policy(target=p),
        [(p.CI,'storage.uploader'),(p.GATEWAY_ACCOUNT,'storage.viewer')]),
    (delivery.BUCKET, delivery.BUCKET_ID, delivery.FOLDER, delivery.policy(production_reader=True), [(p.CI,'storage.viewer')]),
    (s.WEB_BUCKET, s.WEB_BUCKET_ID, s.FOLDER, web_policy(production_reader=True), [(p.CI,'storage.viewer')]),
]


def bucket_api(yc, identifier, action, body=None):
    if identifier not in {item[1] for item in BUCKETS} or action not in {'listAccessBindings','updateAccessBindings'}:
        raise ValueError('Endpoint вне плана бакетов production')
    request=Request('https://storage.api.cloud.yandex.net/storage/v1/buckets/'+identifier+':'+action,
        method='GET' if body is None else 'PATCH', data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+yc.token(),'Content-Type':'application/json'})
    with build_opener(NoRedirect()).open(request,timeout=30) as response:
        return json.load(response)


def resource(yc, group, command, identifier):
    if group[0]=='serverless':
        return cloud(yc,*group,command,'--id',identifier)
    return yc.run(*group,command,identifier)


def plan(yc):
    if yc.run('iam','whoami') != delivery.OPERATOR:
        raise ValueError('Используй согласованного оператора')
    if yc.run('resource-manager','folder','get',p.FOLDER).get('cloud_id') != 'b1ghmudqihbvpnscd2fq':
        raise ValueError('Production находится в другом облаке')
    actors={p.CI,p.API_ACCOUNT,p.GATEWAY_ACCOUNT,p.MIGRATOR}
    for group,identifier,allowed in [
        (('resource-manager','cloud'),'b1ghmudqihbvpnscd2fq',set()),
        (('organization-manager','organization'),'bpf0t8ve1h1ckdn59bot',set()),
        (('resource-manager','folder'),p.FOLDER,{'auditor'}),
        (('resource-manager','folder'),s.FOLDER,set()),
        (('resource-manager','folder'),delivery.FOLDER,set()),
    ]:
        bindings=yc.run(*group,'list-access-bindings',identifier)
        for binding in bindings:
            if binding.get('subject',{}).get('id') in actors and binding['role_id'] not in allowed:
                raise ValueError('Слишком широкие наследуемые права production')
    missing=[]
    resources={}
    for group,identifier,role,actor in BINDINGS:
        key=(group,identifier)
        if key not in resources:
            resources[key]=(resource(yc,group,'get',identifier),resource(yc,group,'list-access-bindings',identifier))
        actual,bindings=resources[key]
        for other in actors:
            allowed_other={r for g,i,r,a in BINDINGS if g==group and i==identifier and a==other}
            if any(b.get('subject',{}).get('id')==other and b['role_id'] not in allowed_other for b in bindings):
                raise ValueError('Лишние права production-субъекта на целевом ресурсе')
        if group==('container','registry'):
            valid=actual.get('id')==delivery.REGISTRY and actual.get('folder_id')==delivery.FOLDER and actual.get('name')=='gym-releases'
        else:
            valid=actual.get('folder_id')==p.FOLDER and actual.get('name') in {
                'gym','gym-api','gym-gateway','gym-migrations','gym-production-api','gym-production-gateway','gym-production-migrations'}
        if not valid: raise ValueError('Ресурс не соответствует фиксированному production')
        allowed={r for g,i,r,a in BINDINGS if g==group and i==identifier and a==actor}
        own={b['role_id'] for b in bindings if b.get('subject',{}).get('id')==actor}
        if own-allowed or any(b.get('subject',{}).get('type')=='system' for b in bindings):
            raise ValueError('Лишние/публичные права на ресурсе production')
        if role not in own: missing.append({'group':list(group),'resource':identifier,'role':role,'account':actor})
    buckets=[]
    for name,identifier,folder,policy,roles in BUCKETS:
        actual=yc.run('storage','bucket','get',name)
        if (actual.get('resource_id')!=identifier or actual.get('folder_id')!=folder
                or actual.get('acl',{}).get('grants') or actual.get('anonymous_access_flags')!={'read':False,'list':False,'config_read':False}):
            raise ValueError('Бакет не соответствует закрытому ресурсу Gym')
        current=actual.get('policy')
        if isinstance(current,str): current=json.loads(current)
        known=(None,policy) if name==p.WEB_BUCKET else (policy,delivery.policy()) if name==delivery.BUCKET else (policy,web_policy())
        if current not in known: raise ValueError('Не заменяй неизвестную bucket policy')
        bindings=bucket_api(yc,identifier,'listAccessBindings')
        if bindings.get('nextPageToken') or any(b.get('subject',{}).get('type')=='system' for b in bindings.get('accessBindings',[])):
            raise ValueError('Публичный или неполный IAM бакета')
        additions=[]
        for other in actors:
            allowed_other={r for a,r in roles if a==other}
            if any(b.get('subject',{}).get('id')==other and b['roleId'] not in allowed_other for b in bindings.get('accessBindings',[])):
                raise ValueError('Лишние права production-субъекта на бакете')
        for actor,role in roles:
            own={b['roleId'] for b in bindings.get('accessBindings',[]) if b.get('subject',{}).get('id')==actor}
            if own-{role}: raise ValueError('Лишняя роль production на бакете')
            if role not in own: additions.append({'roleId':role,'subject':{'type':'serviceAccount','id':actor}})
        buckets.append({'name':name,'resource':identifier,'set_policy':current!=policy,'add_bindings':additions})
    with connect(p.ydb_config(),yc.token()) as driver:
        acl=acl_plan(driver,p.ydb_config())
        for actor in (p.CI,p.GATEWAY_ACCOUNT):
            # CI/Gateway не должны получить доступ к данным через существующий ACL.
            config=p.ydb_config()
            config['accounts']=config['accounts']|{'api':actor}
            acl_plan(driver,config)
    return {'environment':'production','bindings':missing,'buckets':buckets,'acl':acl,
        'limits':'editor включает удаление целевых ресурсов; actAs трёх production SA; DDL/GRANT только мигратору; CI без прямых YDB-прав'}


def apply(yc):
    changes=plan(yc)
    # Сначала ограничивающие политики, потом базовые IAM-привязки.
    for item in changes['buckets']:
        if item['set_policy']:
            policy=next(value[3] for value in BUCKETS if value[0]==item['name'])
            with tempfile.TemporaryDirectory(prefix='gym-production-policy-') as directory:
                path=Path(directory)/'policy.json'
                path.write_text(json.dumps(policy),encoding='utf-8')
                yc.run('storage','bucket','update',item['name'],'--policy-from-file',str(path))
        actual=yc.run('storage','bucket','get',item['name']).get('policy')
        if isinstance(actual,str): actual=json.loads(actual)
        expected=next(value[3] for value in BUCKETS if value[0]==item['name'])
        if actual!=expected: raise ValueError('Ограничивающая политика не подтверждена')
        for binding in item['add_bindings']:
            operation=bucket_api(yc,item['resource'],'updateAccessBindings',{'accessBindingDeltas':[{'action':'ADD','accessBinding':binding}]})
            wait_bucket(yc,operation)
    for item in changes['bindings']:
        args=[*item['group'],'add-access-binding']
        args+=['--id',item['resource']] if item['group'][0]=='serverless' else [item['resource']]
        args+=['--role',item['role'],'--service-account-id',item['account']]
        cloud(yc,*args) if item['group'][0]=='serverless' else yc.run(*args)
    with connect(p.ydb_config(),yc.token()) as driver:
        for action in changes['acl']: apply_acl(driver,action)
    confirmed=plan(yc)
    if confirmed['bindings'] or confirmed['acl'] or any(b['set_policy'] or b['add_bindings'] for b in confirmed['buckets']):
        raise ValueError('Изменения production не подтверждены')
    return confirmed


def main():
    if os.environ.get('GITHUB_ACTIONS'): raise ValueError('Права применяет только локальный оператор')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    yc=Yc(args.yc)
    print(json.dumps(apply(yc) if args.apply else plan(yc),ensure_ascii=False,indent=2))


if __name__=='__main__':
    try: main()
    except Exception as error:
        print('Настройка production остановлена: '+type(error).__name__)
        raise SystemExit(1)
