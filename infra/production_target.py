"""Фиксированные цели production; не принимай ID ресурсов из входов workflow."""

NAME = 'production'
FOLDER = 'b1gt4o1311mhe57522j8'
CI = 'aje5mg9tp1cachquiqlf'
MIGRATOR = 'aje6vfq5em1pla00753d'
CONTAINER = 'bbajpudq2n1mop7e22e9'
URL = 'https://bbajpudq2n1mop7e22e9.containers.yandexcloud.net'
DELIVERY = 'gym-delivery-b1ghmudqihbvpnscd2fq'
DATABASE = 'etn2tjdah1oj7i9055ko'
YDB_ENDPOINT = 'grpcs://ydb.serverless.yandexcloud.net:2135'
YDB_PATH = '/ru-central1/b1ghmudqihbvpnscd2fq/' + DATABASE
FUNCTION = 'd4ee3u6hgmh6hf4ccrpn'
API_ACCOUNT = 'aje8tgptbcm31migrtnj'
GATEWAY_ACCOUNT = 'aje3pgr18tat0pnu4951'
GATEWAY = 'd5dhqvda3eo6r4c2mgf6'
ORIGIN = 'https://d5dhqvda3eo6r4c2mgf6.764nr5vy.apigw.yandexcloud.net'
WEB_BUCKET = 'gym-production-b1ghmudqihbvpnscd2fq'
WEB_BUCKET_ID = 'e3enb50a0kjsmuqhes4t'


def worker_environment(release_id, mode='worker'):
    if mode not in {'worker', 'bootstrap'}:
        raise ValueError('Автоматический recovery запрещён')
    return {'GYM_YDB_ENDPOINT': YDB_ENDPOINT, 'GYM_YDB_DATABASE': YDB_PATH,
        'GYM_YDB_AUTH': 'metadata', 'GYM_RELEASE_ID': release_id,
        'GYM_TABLE_PREFIX': 'gym_', 'GYM_WORKER_MODE': mode}


def ydb_config():
    return {'environment': NAME, 'cloud_id': 'b1ghmudqihbvpnscd2fq', 'folder_id': FOLDER,
        'database_id': DATABASE, 'endpoint': YDB_ENDPOINT, 'database': YDB_PATH,
        'accounts': {'api': API_ACCOUNT, 'migrations': MIGRATOR}, 'runtime_tables': []}
