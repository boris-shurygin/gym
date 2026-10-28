"""Фиксированные цели миграций staging; не принимай ID окружения из запроса."""

FOLDER = 'b1ghdeobqrevsim6jg9o'
CI = 'aje1ia9cf3t1g7o5p6pg'
MIGRATOR = 'ajedn74q9psjfecqnteg'
CONTAINER = 'bba0n53on27kvvqler17'
URL = 'https://bba0n53on27kvvqler17.containers.yandexcloud.net'
DELIVERY = 'gym-delivery-b1ghmudqihbvpnscd2fq'
YDB_ENDPOINT = 'grpcs://ydb.serverless.yandexcloud.net:2135'
YDB_PATH = '/ru-central1/b1ghmudqihbvpnscd2fq/etn63tldguvkj5kcpi3r'


def worker_environment(release_id, mode='worker'):
    if mode not in {'worker', 'bootstrap'}:
        raise ValueError('Автоматический recovery запрещён')
    return {'GYM_YDB_ENDPOINT': YDB_ENDPOINT, 'GYM_YDB_DATABASE': YDB_PATH,
            'GYM_YDB_AUTH': 'metadata', 'GYM_RELEASE_ID': release_id,
            'GYM_TABLE_PREFIX': 'gym_', 'GYM_WORKER_MODE': mode}
