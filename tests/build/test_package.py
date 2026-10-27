import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.build_api import normalize_records, write_archive


class PackageTests(unittest.TestCase):
    def test_records_exclude_missing_cli_wrappers_with_temporary_shebang_hashes(self):
        files = {'library/module.py': b'code', 'library.dist-info/RECORD':
                 b'../../bin/tool,sha256=temporary-path-dependent,12\nlibrary/module.py,sha256=stable,4\nlibrary.dist-info/RECORD,,\n'}
        normalize_records(files)
        self.assertNotIn(b'temporary-path-dependent', files['library.dist-info/RECORD'])
        self.assertIn(b'library/module.py,sha256=stable,4', files['library.dist-info/RECORD'])

    def test_archive_is_reproducible_with_different_input_order(self):
        files = {'index.py': b'handler = None\n', 'api/data.json': b'{}', 'native.so': b'ELF-fixture'}
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'first.zip', Path(directory) / 'second.zip'
            size = write_archive(files, first)
            write_archive(dict(reversed(list(files.items()))), second)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            self.assertEqual(size, sum(map(len, files.values())))
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), sorted(files))
                self.assertNotIn('requirements.txt', archive.namelist())
                self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist()))

    def test_installer_manifest_missing_entrypoint_and_unsafe_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for files in [{}, {'index.py': b'', 'requirements.txt': b'package'},
                          {'index.py': b'', '../secret': b''}, {'index.py': b'', '/absolute': b''},
                          {'index.py': b'', 'api\\secret': b''}]:
                with self.subTest(files=list(files)):
                    with self.assertRaises(ValueError):
                        write_archive(files, Path(directory) / 'bad.zip')
