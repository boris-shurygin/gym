"""Запусти локальный HTTP-адаптер: python -m api.dev --port 8080."""

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from urllib.parse import urlsplit

from api.handler import handler
from contracts.canonical import MAX_BYTES


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        # Стандартный журнал включает URL; используй очищенный gym.http.
        pass

    def _handle(self):
        lengths = self.headers.get_all('Content-Length', [])
        invalid = len(lengths) > 1 or self.headers.get('Transfer-Encoding') is not None
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            length, invalid = 0, True
        if length < 0:
            invalid = True
        if invalid:
            event = None
        elif length > MAX_BYTES:
            event = {'httpMethod': self.command, 'path': '/', 'body': 'x' * (MAX_BYTES + 1)}
        else:
            event = {
                'httpMethod': self.command,
                'path': urlsplit(self.path).path,
                'multiValueHeaders': {key: self.headers.get_all(key) for key in self.headers.keys()},
                'body': base64.b64encode(self.rfile.read(length)).decode('ascii'),
                'isBase64Encoded': True,
            }
        result = handler(event)
        raw = result['body'].encode('utf-8')
        self.send_response(result['statusCode'])
        for key, value in result['headers'].items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    with ThreadingHTTPServer(('127.0.0.1', options.port), Handler) as server:
        print(f'Gym API: http://127.0.0.1:{server.server_port}/api/v1/health', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
