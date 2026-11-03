import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from infra.deployment_diagnostics import phase
from infra.object_store import StorageError


class DeploymentDiagnosticsTests(unittest.TestCase):
    def test_http_failure_keeps_request_id_but_never_credentials_or_response_body(self):
        body = io.BytesIO(b'private-response-secret')
        error = HTTPError('https://user:password@storage.yandexcloud.net/bucket/blob?signature=query-secret#fragment-secret',
            500, 'private-error-message', {'X-Amz-Request-Id': 'request-123',
                'Authorization': 'Bearer header-secret', 'Set-Cookie': 'cookie-secret',
                'X-Request-Id': 'unsafe\nheader-secret'}, body)
        operation = Mock(side_effect=error)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaises(HTTPError) as caught:
                with phase('load_payload'):
                    operation()
        self.assertIs(caught.exception, error)
        operation.assert_called_once_with()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        failed = events[-1]
        self.assertEqual(failed['phase'], 'load_payload')
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['http_status'], 500)
        self.assertEqual(failed['host'], 'storage.yandexcloud.net')
        self.assertEqual(failed['path'], '/bucket/blob')
        self.assertEqual(failed['request_ids'], {'x-amz-request-id': 'request-123'})
        self.assertEqual(body.tell(), 0)
        for private in ('password', 'query-secret', 'fragment-secret', 'header-secret',
                'cookie-secret', 'private-response-secret', 'private-error-message'):
            self.assertNotIn(private, output.getvalue())

    def test_wrapped_storage_failure_retains_http_details_without_rewriting_exception(self):
        original = HTTPError('https://storage.yandexcloud.net/bucket/blob', 503, 'secret', {}, None)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaises(StorageError):
                with phase('read_manifest'):
                    try:
                        raise original
                    except HTTPError:
                        raise StorageError('private SDK details') from None
        failed = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(failed['error_type'], 'StorageError')
        self.assertEqual(failed['http_status'], 503)
        self.assertEqual(failed['host'], 'storage.yandexcloud.net')
        self.assertNotIn('private SDK details', output.getvalue())

    def test_invalid_error_url_does_not_mask_original_failure(self):
        error = HTTPError('https://[invalid', 500, 'secret', {}, None)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaises(HTTPError) as caught:
                with phase('staging_token'):
                    raise error
        self.assertIs(caught.exception, error)
        failed = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(failed['http_status'], 500)
        self.assertNotIn('host', failed)

    def test_output_failure_does_not_repeat_or_replace_operation(self):
        error = HTTPError('https://api.github.com/repos/owner/repo', 500, 'secret', {}, None)
        operation = Mock(side_effect=error)
        with patch('builtins.print', side_effect=OSError('broken output')):
            with self.assertRaises(HTTPError) as caught:
                with phase('github_context'):
                    operation()
        self.assertIs(caught.exception, error)
        operation.assert_called_once_with()
