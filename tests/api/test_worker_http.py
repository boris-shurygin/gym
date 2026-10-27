from http.client import HTTPConnection
from http.server import HTTPServer
import json
import threading
import unittest
from unittest.mock import Mock, patch

from api.database import DatabaseSettings
from db.migrate import MigrationBusy
from db.worker import RepairRequired, Worker
from db.worker_http import Application, Settings, handler_for


class WorkerHttpTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(DatabaseSettings('grpc://localhost:2136', '/local', 'anonymous'), 'validation')
        self.worker = Mock(release='validation', digest='a' * 64)
        self.app = Application(self.settings, self.worker)

    def body(self, operation='step', **extra):
        result = {'operation': operation, 'release_id': 'validation', 'checksum': 'a' * 64}
        if operation in {'step', 'status'}:
            result['job_id'] = 'job'
        return result | extra

    def test_only_bound_inputs_are_accepted(self):
        for extra in ({'sql': 'DROP TABLE x'}, {'database': '/other'}, {'release_id': 'other'}, {'checksum': 'b' * 64}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.app.call(self.body(**extra))
        self.worker.step.assert_not_called()
        self.app.call(self.body())
        self.worker.step.assert_called_once_with('job', after_acquire=None)

    def test_normal_mode_cannot_bootstrap_or_recover(self):
        for operation in ('bootstrap', 'recover'):
            with self.assertRaises(ValueError):
                self.app.call(self.body(operation))
        self.worker.bootstrap.assert_not_called()
        self.worker.recover.assert_not_called()

    def test_recovery_uses_only_deployment_ticket(self):
        ticket = {'job_id': 'job', 'owner': '1' * 32, 'generation': 1, 'not_before': 1, 'evidence_sha256': '2' * 64}
        settings = Settings(self.settings.database, 'validation', mode='recovery', recovery_ticket=ticket)
        app = Application(settings, self.worker)
        app.call(self.body('recover'))
        self.worker.recover.assert_called_once_with(ticket)
        with self.assertRaises(ValueError):
            app.call(self.body('recover', ticket=ticket))
        with self.assertRaises(ValueError):
            app.call(self.body('step'))

    def test_crash_probe_is_not_available_outside_synthetic_staging(self):
        with self.assertRaises(ValueError):
            Settings(self.settings.database, 'validation', crash_after_acquire=True)

    def test_recovery_validates_before_database(self):
        worker = Worker(Mock(), 'validation')
        for ticket in ({}, {'job_id': 'x', 'owner': '1' * 32, 'generation': True,
                            'not_before': 1, 'evidence_sha256': '2' * 64}):
            with self.assertRaises(ValueError):
                worker.recover(ticket)

    def test_real_http_health_errors_and_secret_redaction(self):
        server = HTTPServer(('127.0.0.1', 0), handler_for(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None):
            client = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                client.request(method, path, body=body, headers={'Content-Type': 'application/json'})
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        status, result = request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertEqual(result['checksum'], 'a' * 64)
        self.worker.status.assert_not_called()
        for error, expected in ((ValueError('secret'), 400), (MigrationBusy('secret'), 409),
                                (RepairRequired('secret'), 409), (RuntimeError('token-secret sql-secret'), 503)):
            self.worker.step.side_effect = error
            with patch('builtins.print') as log:
                status, result = request('POST', '/v1/migrations', json.dumps(self.body()))
            self.assertEqual(status, expected)
            self.assertNotIn('secret', json.dumps(result) + str(log.call_args_list))
        self.assertEqual(request('POST', '/v1/migrations', b'{"operation":1,"operation":2}')[0], 400)
        self.assertEqual(request('POST', '/v1/migrations', b'x' * 16385)[0], 413)
        self.assertEqual(request('GET', '/api/v1/health')[0], 404)


if __name__ == '__main__':
    unittest.main()
