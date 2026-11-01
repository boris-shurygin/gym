"""Внешняя проверка staging без cookie, ключей и доступа GitHub к данным YDB."""
import json
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler

from infra.staging_target import ORIGIN


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe():
    try:
        with build_opener(NoRedirect()).open(Request(ORIGIN + '/api/v1/health/ready',
                headers={'User-Agent': 'Gym-pilot-health'}), timeout=40) as response:
            return response.status == 200 and json.loads(response.read(1024)) == {'status': 'ok'}
    except Exception:
        return False


def main():
    for attempt in range(3):
        if probe():
            print(json.dumps({'environment': 'staging', 'ready': True, 'attempt': attempt + 1}))
            return
        if attempt < 2:
            time.sleep(10)
    print(json.dumps({'environment': 'staging', 'ready': False, 'attempts': 3}))
    raise SystemExit(1)


if __name__ == '__main__':
    main()
