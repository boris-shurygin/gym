"""Создай только служебную схему production проверенным worker из авторизованной среды оператора."""

import argparse
import json
import os

from infra import production_target as target, production_gate, staging_migrations
from infra.delivery_access import OPERATOR
from infra.ydb_access import Yc


def main():
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Первоначальный bootstrap выполняет только оператор')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc',required=True)
    parser.add_argument('--release',required=True)
    args=parser.parse_args()
    yc=Yc(args.yc)
    if yc.run('iam','whoami')!=OPERATOR:
        raise ValueError('Используй согласованного оператора')
    manifest,receipt=production_gate.staged_release(yc.token(),args.release)
    if manifest['migration']['revision']!='0001_runtime_metadata':
        raise ValueError('Неизвестный bootstrap схемы')
    result=staging_migrations.execute(yc.token(),args.release,receipt['manifest_sha256'],bootstrap=True,target=target)
    print(json.dumps(result))


if __name__=='__main__':
    try: main()
    except Exception as error:
        print('Bootstrap production остановлен: '+type(error).__name__)
        raise SystemExit(1)
