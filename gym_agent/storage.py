"""Храни токены в DPAPI или в разблокированном Linux Secret Service."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
_STAGING_ORIGIN = 'https://d5d734k3s76t42pa5rtn.764nr5vy.apigw.yandexcloud.net'


class ClientError(Exception):
    def __init__(self, code, trace_id=None):
        super().__init__(code)
        self.code, self.trace_id = code, trace_id


def protect(raw, decrypt=False):
    if os.name != 'nt':
        raise ClientError('secure_storage_requires_windows')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, result = Blob(len(raw), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ClientError('credential_storage_failed')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
        kernel.LocalFree(result.data)


class Vault:
    def __init__(self, environment='staging', *, origin=None):
        if environment not in ('staging', 'production'):
            raise ClientError('invalid_environment')
        if origin is None and environment == 'staging':
            origin = _STAGING_ORIGIN
        parsed = urlsplit(origin or '')
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ClientError('invalid_environment_origin')
        self.environment = environment
        self.path = ROOT / '.local' / 'agent-credentials' / (environment + '.dat')
        # Match the per-checkout refresh lock, including when two checkouts use
        # the same user's Secret Service; sharing a token would race rotation.
        self.attributes = {'application': 'gym-python', 'environment': environment,
            'resource': origin + '/mcp', 'client_id': 'gym-python',
            'workspace': hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()}

    @staticmethod
    def _platform():
        if os.name == 'nt':
            return 'windows'
        if sys.platform.startswith('linux'):
            return 'linux'
        raise ClientError('secure_storage_platform_unsupported')

    @contextmanager
    def _collection(self):
        try:
            import secretstorage
            from secretstorage.util import open_session
        except ImportError:
            raise ClientError('secure_storage_dependency_missing') from None
        connection = None
        try:
            connection = secretstorage.dbus_init()
            session = open_session(connection)
            if session.encrypted is not True:
                raise ClientError('secure_storage_encryption_required')
            # get_default_collection() silently creates a missing collection.
            # Construct its default alias directly to avoid creation or prompts.
            collection = secretstorage.Collection(connection, session=session)
            if collection.is_locked():
                raise ClientError('secure_storage_locked')
            yield collection
        except ClientError:
            raise
        except secretstorage.ItemNotFoundException:
            raise ClientError('secure_storage_collection_missing') from None
        except secretstorage.LockedException:
            raise ClientError('secure_storage_locked') from None
        except Exception:
            # D-Bus errors can contain service details; never expose their text.
            raise ClientError('secure_storage_unavailable') from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _item(self, collection):
        # GNOME Keyring adds the standard schema metadata to stored items.
        # Keep every Gym namespace attribute exact and reject other extras.
        items = [item for item in collection.search_items(self.attributes)
            if {key: value for key, value in item.get_attributes().items()
                if key != 'xdg:schema'} == self.attributes]
        if len(items) > 1:
            raise ClientError('credential_storage_ambiguous')
        if items and items[0].is_locked():
            raise ClientError('secure_storage_locked')
        return items[0] if items else None

    def check_available(self):
        if self._platform() == 'windows':
            protect(b'Gym secure storage check')
        else:
            with self._collection():
                pass

    def _directory(self):
        # Refuse symlinks instead of following them or modifying their targets.
        for path in (self.path.parent, *self.path.parent.parents):
            if path.is_symlink():
                raise ClientError('insecure_credential_storage')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._platform() == 'linux':
            info = self.path.parent.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ClientError('insecure_credential_storage')

    @contextmanager
    def locked(self):
        platform = self._platform()
        self._directory()
        lock_path = self.path.with_suffix('.lock')
        if lock_path.is_symlink():
            raise ClientError('insecure_credential_storage')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError:
            raise ClientError('insecure_credential_storage') from None
        with os.fdopen(descriptor, 'r+b') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or platform == 'linux' and (info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077)):
                raise ClientError('insecure_credential_storage')
            if platform == 'windows':
                import msvcrt

                def acquire():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

                def release():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def acquire():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def release():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

            deadline = time.monotonic() + 10
            while True:
                try:
                    acquire()
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise ClientError('credential_storage_failed') from None
                    if time.monotonic() >= deadline:
                        raise ClientError('another_agent_is_refreshing') from None
                    time.sleep(0.1)
            try:
                yield
            finally:
                release()

    def exists(self):
        if self._platform() == 'windows':
            if self.path.is_symlink():
                raise ClientError('insecure_credential_storage')
            return self.path.is_file()
        with self._collection() as collection:
            return self._item(collection) is not None

    def read(self):
        if self._platform() == 'windows':
            if not self.exists():
                raise ClientError('login_required')
            raw = protect(self.path.read_bytes(), True)
        else:
            with self._collection() as collection:
                item = self._item(collection)
                if item is None:
                    raise ClientError('login_required')
                raw = item.get_secret()
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            raise ClientError('credential_storage_invalid') from None
        if not isinstance(value, dict):
            raise ClientError('credential_storage_invalid')
        return value

    def save(self, value):
        raw = json.dumps(value).encode()
        if self._platform() == 'windows':
            self._directory()
            if self.path.is_symlink():
                raise ClientError('insecure_credential_storage')
            temporary = self.path.with_suffix('.tmp-' + secrets.token_hex(8))
            try:
                with temporary.open('xb') as handle:
                    handle.write(protect(raw))
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            with self._collection() as collection:
                item = self._item(collection)
                if item is not None:
                    item.set_secret(raw, content_type='application/json')
                else:
                    collection.create_item('Gym Python ' + self.environment, self.attributes,
                        raw, replace=False, content_type='application/json')
