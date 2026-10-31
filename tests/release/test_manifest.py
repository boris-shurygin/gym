import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from infra.release_artifacts import SCHEMA, create_manifest, publish, verify_bundle


class RecordingStore:
    def __init__(self, fail=False):
        self.writes, self.fail = [], fail

    def put(self, key, data):
        if self.fail:
            raise RuntimeError('Сбой загрузки')
        self.writes.append((key, data))
        return {'key': key, 'state': 'created'}


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        temporary_root = Path(self.directory.name).resolve()
        assert temporary_root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        self.root = temporary_root
        api = io.BytesIO()
        with zipfile.ZipFile(api, 'w') as archive:
            archive.writestr('index.py', 'handler = None\n')
        fixtures = {'api.zip': api.getvalue(), 'gateway.json': b'{"openapi":"3.0.0","paths":{}}',
                    'web/index.html': b'<!doctype html><html>fixture</html>',
                    'web/service-worker.js': b'/* synthetic public shell */',
                    'web/manifest.webmanifest': b'{"start_url":"/","scope":"/"}',
                    'web/assets/app-hash.js': b'console.log("fixture")'}
        for name, body in fixtures.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.manifest = create_manifest(self.root, 'a' * 40, 123, 1,
            'cr.yandex/crpe91o8ka9e03n3n90c/gym-migrations@sha256:' + 'b' * 64, '0001_runtime_metadata')

    def test_schema_and_complete_fixture(self):
        Draft202012Validator.check_schema(SCHEMA)
        files = verify_bundle(self.manifest, self.root)
        self.assertEqual(len(files), 6)
        self.assertEqual(self.manifest['release_id'], 'a' * 40 + '_123_1')
        index = next(a for a in self.manifest['artifacts'] if a['path'] == 'web/index.html')
        self.assertEqual(index['cache_control'], 'no-cache')

    def test_actual_project_pwa_manifest_preserves_same_origin(self):
        path = self.root / 'web/manifest.webmanifest'
        actual = json.loads((Path(__file__).parents[2] / 'web/public/manifest.webmanifest').read_text(encoding='utf-8'))
        path.write_text(json.dumps(actual), encoding='utf-8')
        manifest = create_manifest(self.root, 'a' * 40, 123, 1, self.manifest['migration']['image'], '0001')
        verify_bundle(manifest, self.root)
        for start in ('https://foreign.example/', '//foreign.example/', '/other', '/#/unknown'):
            with self.subTest(start=start):
                path.write_text(json.dumps(actual | {'start_url': start}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    create_manifest(self.root, 'a' * 40, 123, 1, self.manifest['migration']['image'], '0001')

    def test_tampered_missing_or_extra_file_prevents_every_write(self):
        for change in ['alter', 'remove', 'extra']:
            with self.subTest(change=change):
                original = self.root / 'web/index.html'
                body = original.read_bytes()
                if change == 'alter':
                    original.write_bytes(body + b'changed')
                elif change == 'remove':
                    original.unlink()
                else:
                    (self.root / 'web/extra.js').write_bytes(b'extra')
                store = RecordingStore()
                with self.assertRaises(ValueError):
                    publish(self.manifest, self.root, store)
                self.assertEqual(store.writes, [])
                original.write_bytes(body)
                if change == 'extra':
                    (self.root / 'web/extra.js').unlink()

    def test_manifest_is_last_and_blobs_are_addressed_by_hash(self):
        store = RecordingStore()
        result = publish(self.manifest, self.root, store)
        self.assertEqual(store.writes[-1][0], 'releases/' + self.manifest['release_id'] + '/manifest.json')
        for key, data in store.writes[:-1]:
            self.assertEqual(key, 'blobs/sha256/' + hashlib.sha256(data).hexdigest())
        self.assertEqual(result['manifest_sha256'], hashlib.sha256(store.writes[-1][1]).hexdigest())
        self.assertEqual(json.loads(store.writes[-1][1]), self.manifest)

    def test_failed_blob_upload_does_not_publish_manifest(self):
        store = RecordingStore(fail=True)
        with self.assertRaises(RuntimeError):
            publish(self.manifest, self.root, store)
        self.assertFalse(any('/manifest.json' in key for key, _ in store.writes))

    def test_invalid_provenance_paths_image_or_headers_rejected(self):
        for change in ['id', 'ref', 'workflow', 'image', 'path', 'duplicate', 'metadata', 'unknown']:
            with self.subTest(change=change):
                manifest = copy.deepcopy(self.manifest)
                if change == 'id': manifest['release_id'] = 'c' * 40 + '_123_1'
                elif change == 'ref': manifest['build']['ref'] = 'refs/heads/pr-branch'
                elif change == 'workflow': manifest['build']['workflow'] = '.github/workflows/other.yml'
                elif change == 'image': manifest['migration']['image'] = 'cr.yandex/crpe91o8ka9e03n3n90c/gym-migrations:latest'
                elif change == 'path': manifest['artifacts'][0]['path'] = '../secret'
                elif change == 'duplicate': manifest['artifacts'].append(manifest['artifacts'][0])
                elif change == 'metadata': manifest['artifacts'][0]['content_type'] = 'text/html; charset=utf-8'
                else: manifest['approved'] = True
                store = RecordingStore()
                with self.assertRaises((ValueError, ValidationError)):
                    publish(manifest, self.root, store)
                self.assertEqual(store.writes, [])

    def test_secret_filename_and_service_route_are_not_public_assets(self):
        for name in ['web/.env', 'web/auth/callback.html', 'web/private.pem']:
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'not-public')
                with self.assertRaises(ValueError):
                    create_manifest(self.root, 'a' * 40, 123, 1, self.manifest['migration']['image'], '0001')
                path.unlink()

    def test_zip_installer_or_traversal_is_rejected_even_with_matching_hash(self):
        for name in ['requirements.txt', '../escape.py']:
            with self.subTest(name=name):
                api = io.BytesIO()
                with zipfile.ZipFile(api, 'w') as archive:
                    archive.writestr('index.py', 'handler = None')
                    archive.writestr(name, 'unsafe')
                (self.root / 'api.zip').write_bytes(api.getvalue())
                with self.assertRaises(ValueError):
                    create_manifest(self.root, 'a' * 40, 123, 1, self.manifest['migration']['image'], '0001')

    def test_zip_crc_and_pwa_same_prefix_file_order(self):
        (self.root / 'web/assets.js').write_bytes(b'root asset')
        manifest = create_manifest(self.root, 'a' * 40, 123, 1, self.manifest['migration']['image'], '0001')
        names = [a['path'] for a in manifest['artifacts']]
        self.assertEqual(names, sorted(names))
        archive = (self.root / 'api.zip').read_bytes().replace(b'handler = None', b'Handler = None')
        (self.root / 'api.zip').write_bytes(archive)
        with self.assertRaises(ValueError):
            create_manifest(self.root, 'a' * 40, 123, 1, self.manifest['migration']['image'], '0001')
