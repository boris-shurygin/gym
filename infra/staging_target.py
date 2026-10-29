"""Фиксированные цели приложения/миграций staging; не принимай ID окружения из запроса."""

NAME = 'staging'
FOLDER = 'b1ghdeobqrevsim6jg9o'
CI = 'aje1ia9cf3t1g7o5p6pg'
MIGRATOR = 'ajedn74q9psjfecqnteg'
CONTAINER = 'bba0n53on27kvvqler17'
URL = 'https://bba0n53on27kvvqler17.containers.yandexcloud.net'
DELIVERY = 'gym-delivery-b1ghmudqihbvpnscd2fq'
YDB_ENDPOINT = 'grpcs://ydb.serverless.yandexcloud.net:2135'
YDB_PATH = '/ru-central1/b1ghmudqihbvpnscd2fq/etn63tldguvkj5kcpi3r'
FUNCTION = 'd4efel2j20be6tq27cr1'
API_ACCOUNT = 'ajeffa6opd0qko82m4de'
GATEWAY_ACCOUNT = 'ajedmh6ct8jnm9rac6hm'
GATEWAY = 'd5d734k3s76t42pa5rtn'
ORIGIN = 'https://d5d734k3s76t42pa5rtn.764nr5vy.apigw.yandexcloud.net'
WEB_BUCKET = 'gym-staging-b1ghmudqihbvpnscd2fq'
WEB_BUCKET_ID = 'e3epqr6e0gmajs4mp6sf'


def worker_environment(release_id, mode='worker'):
    if mode not in {'worker', 'bootstrap'}:
        raise ValueError('Автоматический recovery запрещён')
    return {'GYM_YDB_ENDPOINT': YDB_ENDPOINT, 'GYM_YDB_DATABASE': YDB_PATH,
            'GYM_YDB_AUTH': 'metadata', 'GYM_RELEASE_ID': release_id,
            'GYM_TABLE_PREFIX': 'gym_', 'GYM_WORKER_MODE': mode}
