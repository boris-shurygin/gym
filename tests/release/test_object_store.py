import hashlib
import io
import unittest
from urllib.error import HTTPError, URLError

from infra.object_store import ArtifactConflict, ImmutableStore, NoRedirect, StorageError


class Response(io.BytesIO):
    def __init__(self, data=b'', *, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


def http_error(status):
    return HTTPError('https://storage.yandexcloud.net/bucket/key', status, 'error', {}, io.BytesIO(b'not logged'))


class SequenceOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class StoreTests(unittest.TestCase):
    def store(self, *responses):
        opener = SequenceOpener(*responses)
        return ImmutableStore('synthetic-iam-token', 'fixture-bucket', opener=opener), opener

    def test_put_is_conditional_and_uses_iam_with_checksum(self):
        store, opener = self.store(Response())
        result = store.put('blobs/sha256/fixture', b'fixture')
        headers = {k.lower(): v for k, v in opener.requests[0].header_items()}
        self.assertEqual(headers['if-none-match'], '*')
        self.assertEqual(headers['authorization'], 'Bearer synthetic-iam-token')
        self.assertEqual(headers['x-amz-meta-sha256'], hashlib.sha256(b'fixture').hexdigest())
        self.assertIn('content-md5', headers)
        self.assertEqual(result['state'], 'created')

    def test_precondition_failure_checks_actual_bytes(self):
        store, opener = self.store(http_error(412), Response(b'same'))
        self.assertEqual(store.put('releases/id/manifest.json', b'same')['state'], 'verified')
        self.assertEqual([r.method for r in opener.requests], ['PUT', 'GET'])

    def test_matching_metadata_does_not_hide_changed_body(self):
        store, opener = self.store(http_error(412), Response(b'evil', headers={
            'Content-Length': '4', 'X-Amz-Meta-Sha256': hashlib.sha256(b'same').hexdigest()}))
        with self.assertRaises(ArtifactConflict):
            store.put('blobs/sha256/fixture', b'same')
        self.assertEqual(len(opener.requests), 2)

    def test_lost_put_response_recovers_by_readback(self):
        store, opener = self.store(URLError('lost response'), Response(b'committed'))
        self.assertEqual(store.put('blobs/sha256/fixture', b'committed')['state'], 'verified')
        self.assertEqual([r.method for r in opener.requests], ['PUT', 'GET'])

    def test_conflict_and_missing_object_retry_only_conditional_put(self):
        store, opener = self.store(http_error(409), http_error(404), Response())
        self.assertEqual(store.put('blobs/sha256/fixture', b'value')['state'], 'created')
        self.assertEqual([r.method for r in opener.requests], ['PUT', 'GET', 'PUT'])
        for request in opener.requests:
            if request.method == 'PUT':
                self.assertEqual(dict((k.lower(), v) for k, v in request.header_items())['if-none-match'], '*')

    def test_oversized_or_truncated_response_is_conflict(self):
        for body, headers in [(b'too-long', {}), (b'x', {}), (b'same', {'Content-Length': '5'})]:
            store, _ = self.store(http_error(412), Response(body, headers=headers))
            with self.subTest(body=body):
                with self.assertRaises(ArtifactConflict):
                    store.put('blobs/sha256/fixture', b'same')

    def test_redirect_or_denial_does_not_forward_token_or_log_response_body(self):
        for status in [302, 307, 401, 403, 500]:
            store, opener = self.store(http_error(status))
            with self.subTest(status=status):
                with self.assertRaises(StorageError) as error:
                    store.put('blobs/sha256/fixture', b'data')
                self.assertNotIn('synthetic-iam-token', str(error.exception))
                self.assertNotIn('not logged', str(error.exception))
                self.assertEqual(len(opener.requests), 1)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 307, '', {}, 'https://other.example/'))

    def test_bad_key_is_rejected_before_request(self):
        for key in ['../secret', '/root', 'a//b', 'a?token', 'a\\b']:
            store, opener = self.store()
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    store.put(key, b'value')
                self.assertEqual(opener.requests, [])
