"""Дождись IAM-операции Storage через канонический Operation API, без повторного PATCH."""

import json
import re
import time


def wait(yc, operation):
    deadline=time.monotonic()+60
    while not operation.get('done') and not operation.get('error') and time.monotonic()<deadline:
        identifier=operation.get('id','')
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-z0-9]{1,64}',identifier):
            raise ValueError('Неизвестный идентификатор операции IAM')
        time.sleep(2)
        operation=yc.run('operation','get',identifier,'--endpoint','operation.api.cloud.yandex.net:443')
    if not operation.get('done') or operation.get('error'):
        print(json.dumps({'event':'bucket_iam_unconfirmed','operation':operation.get('id'),
            'done':operation.get('done',False),'error_code':operation.get('error',{}).get('code')}))
        raise ValueError('Проверь состояние операции IAM бакета')
