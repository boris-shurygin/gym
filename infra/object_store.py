"""Публикуй неизменяемые объекты условным PUT, без постоянных ключей."""

import base64
import hashlib
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_OBJECT = 128 * 1024 * 1024


class StorageError(RuntimeError):
    pass


class ArtifactConflict(StorageError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Не передавай IAM-токен по Location из ответа сервера.
        return None


class ImmutableStore:
    def __init__(self, token, bucket, *, opener=None):
        if not token or '\n' in token or '\r' in token:
            raise ValueError('Передай временный IAM-токен')
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
            raise ValueError('Некорректное имя бакета')
        self._token, self.bucket = token, bucket
        self._opener = opener or build_opener(NoRedirect())

    def _open(self, method, key, *, data=None, headers=None):
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', key) or any(p in {'', '.', '..'} for p in key.split('/')):
            raise ValueError('Некорректный ключ объекта')
        url = 'https://storage.yandexcloud.net/' + self.bucket + '/' + quote(key, safe='/')
        request = Request(url, method=method, data=data, headers={
            'Authorization': 'Bearer ' + self._token, **(headers or {}),
        })
        return self._opener.open(request, timeout=30)

    def verify(self, key, digest, size):
        try:
            with self._open('GET', key) as response:
                declared = response.headers.get('Content-Length')
                if declared is not None and int(declared) != size:
                    raise ArtifactConflict('Размер занятого объекта отличается')
                actual, count = hashlib.sha256(), 0
                while True:
                    block = response.read(min(65536, size - count + 1))
                    if not block:
                        break
                    actual.update(block)
                    count += len(block)
                    if count > size:
                        raise ArtifactConflict('Занятый объект содержит другие байты')
                if count != size or actual.hexdigest() != digest:
                    raise ArtifactConflict('Хеш занятого объекта отличается')
                return True
        except HTTPError as error:
            status = error.code
            error.close()
            if status == 404:
                return False
            raise StorageError(f'Проверка объекта отклонена: HTTP {status}') from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise StorageError('Не удалось подтвердить содержимое объекта') from None

    def put(self, key, data, *, content_type='application/octet-stream', cache_control='private, max-age=31536000, immutable'):
        if not isinstance(data, bytes) or len(data) > MAX_OBJECT:
            raise ValueError('Ожидаются байты объекта размером до 128 MiB')
        if any(not isinstance(value, str) or not value or '\r' in value or '\n' in value for value in (content_type, cache_control)):
            raise ValueError('Некорректные заголовки объекта')
        digest = hashlib.sha256(data).hexdigest()
        headers = {'If-None-Match': '*', 'Content-Type': content_type,
                   'Content-MD5': base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode('ascii'),
                   'X-Amz-Meta-Sha256': digest, 'Cache-Control': cache_control}
        for attempt in range(3):
            try:
                with self._open('PUT', key, data=data, headers=headers) as response:
                    if response.status not in {200, 201}:
                        raise StorageError('Неожиданный ответ на условный PUT')
                return {'key': key, 'sha256': digest, 'bytes': len(data), 'state': 'created'}
            except HTTPError as error:
                status = error.code
                error.close()
                if status not in {409, 412}:
                    raise StorageError(f'Публикация отклонена: HTTP {status}') from None
            except (URLError, TimeoutError, OSError):
                # Ответ PUT мог потеряться после записи: сначала проверь байты.
                pass
            if self.verify(key, digest, len(data)):
                return {'key': key, 'sha256': digest, 'bytes': len(data), 'state': 'verified'}
        raise StorageError('Условная публикация не подтверждена после трёх попыток')
