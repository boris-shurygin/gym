"""Сохраняй текущую и две предыдущие успешные установки, освобождай теги до deploy."""

import hashlib
import re
from urllib.error import HTTPError

from contracts.canonical import canonical_bytes, parse_request
from infra.object_store import ImmutableStore
from infra.staging_migrations import InterventionRequired, read_manifest


FUNCTIONS = 'https://serverless-functions.api.cloud.yandex.net/functions/v1/'
GATEWAYS = 'https://serverless-apigateway.api.cloud.yandex.net/apigateways/v1/apigateways/'
PREFIX = 'gym-tag-'
KEYS = ('gym-tag-current', 'gym-tag-previous-1', 'gym-tag-previous-2')
RELEASE = re.compile(r'[a-f0-9]{40}_[1-9][0-9]{0,15}_[1-9][0-9]{0,4}')
# Однократный переход с истории до ADR-0018. Порядок подтверждён журналом и квитанциями,
# а не временем создания функции (оно не отражает откаты).
LEGACY = {
    'staging': ('54aee729231f221d2f177e378ea88d9fdfff5a9b_41048520945_2',
                'b138d10f40ab4b2b98d7a81f5a0ceebc702c9bcd_40949467086_1',
                '15a2ad57393a757cd51f1c01952a2885db4c58bb_40909363405_1'),
    'production': ('9a16a2f02a77f5227aa96046d5b0fa26f7d525b1_40852028322_1',),
}


def tag(release):
    if not isinstance(release, str) or not RELEASE.fullmatch(release):
        raise ValueError('Некорректный выпуск истории тегов')
    return 'r-' + hashlib.sha256(release.encode()).hexdigest()[:32]


def history(labels):
    own = {k: v for k, v in labels.items() if k.startswith(PREFIX)}
    if not own:
        return None
    if own.get(PREFIX + 'schema') != '1' or set(own) - {PREFIX + 'schema', *KEYS}:
        raise InterventionRequired('Неизвестный формат истории тегов')
    result = [own[k] for k in KEYS if k in own]
    if not result or any(k not in own for k in KEYS[:len(result)]) or len(set(result)) != len(result):
        raise InterventionRequired('Повреждена история тегов')
    for release in result:
        tag(release)
    return result


def window(release, previous):
    return list(dict.fromkeys([release, *previous]))[:3]


def read_object(store, key, *, optional=False):
    try:
        with store._open('GET', key) as response:
            raw = response.read(1024 * 1024 + 1)
    except HTTPError as error:
        code = error.code
        error.close()
        if optional and code == 404:
            return None
        raise
    if len(raw) > 1024 * 1024:
        raise ValueError('Слишком большая запись выпуска')
    value = parse_request(raw)
    if raw != canonical_bytes(value):
        raise ValueError('Неканоническая запись выпуска')
    return value


class TagRetention:
    def __init__(self, cloud, token, target):
        self.cloud, self.token, self.target = cloud, token, target
        self.function = FUNCTIONS + 'functions/' + target.FUNCTION
        self.store = ImmutableStore(token, target.WEB_BUCKET)
        self.delivery = ImmutableStore(token, target.DELIVERY)
        self.archives = {}

    def labels(self):
        resource = self.cloud.json(self.function)
        if resource.get('name') != 'gym-api' or resource.get('folderId') != self.target.FOLDER:
            raise ValueError('Чужая функция ротации')
        return resource.get('labels', {})

    def versions(self):
        value = self.cloud.json(FUNCTIONS + 'versions?functionId=' + self.target.FUNCTION + '&pageSize=1000')
        if value.get('nextPageToken'):
            raise InterventionRequired('Неполный список версий; теги не изменены')
        return value.get('versions', [])

    def installed(self):
        # Поздний импорт: общий deploy используется также из production_release.
        from infra.production_release import installed
        current, digest = installed(self.cloud, self.target)
        spec = parse_request(self.cloud.json(GATEWAYS + self.target.GATEWAY + ':spec?format=JSON')['openapiSpec'].encode())
        found = []
        def visit(value):
            if isinstance(value, dict):
                if value.get('type') == 'cloud_functions':
                    found.append((value.get('function_id'), value.get('tag')))
                for item in value.values(): visit(item)
            elif isinstance(value, list):
                for item in value: visit(item)
        visit(spec)
        if hashlib.sha256(canonical_bytes(spec)).hexdigest() != digest:
            raise InterventionRequired('Gateway изменился во время чтения')
        if current is not None and (not found or set(found) != {(self.target.FUNCTION, tag(current))}):
            raise InterventionRequired('Маршруты Gateway указывают на разные версии')
        return current, digest

    def receipt(self, release):
        value = read_object(self.store, 'deployments/' + release + '/result.json', optional=True)
        if value is None:
            return None
        expected = {'format': 1, 'environment': self.target.NAME, 'release_id': release,
                    'main_sha': release[:40], 'function_tag': tag(release), 'smoke': 'succeeded',
                    'purpose': self.target.NAME + '-release', 'origin': self.target.ORIGIN}
        if (any(value.get(k) != v for k, v in expected.items())
                or not re.fullmatch(r'[a-z0-9]{1,64}', value.get('function_version', ''))
                or any(not re.fullmatch(r'[a-f0-9]{64}', value.get(k, ''))
                       for k in ('manifest_sha256', 'gateway_sha256'))):
            raise InterventionRequired('Не подтверждена квитанция окружения')
        return value

    def archive(self, release, receipt):
        if release not in self.archives:
            manifest = read_object(self.delivery, 'releases/' + release + '/manifest.json')
            digest = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
            if receipt is not None and digest != receipt['manifest_sha256']:
                raise InterventionRequired('Архив отличается от установленного выпуска')
            self.archives[release] = read_manifest(self.token, release, digest)
        elif receipt is not None and hashlib.sha256(canonical_bytes(self.archives[release])).hexdigest() != receipt['manifest_sha256']:
            raise InterventionRequired('Архив отличается от квитанции')
        return self.archives[release]

    def owned(self, version, release, receipt):
        env = version.get('environment', {})
        manifest = self.archive(release, receipt)
        digest = next(a['sha256'] for a in manifest['artifacts'] if a['path'] == 'api.zip')
        if (version.get('functionId') != self.target.FUNCTION or version.get('status') != 'ACTIVE'
                or version.get('serviceAccountId') != self.target.API_ACCOUNT
                or env.get('GYM_ENVIRONMENT') != self.target.NAME or env.get('GYM_RELEASE_ID') != release
                or env.get('GYM_API_ZIP_SHA256') != digest
                or (receipt is not None and version['id'] != receipt['function_version'])):
            raise InterventionRequired('Тег не принадлежит подтверждённой версии Gym')

    def confirmed(self, release, versions):
        receipt = self.receipt(release)
        found = [v for v in versions if tag(release) in v.get('tags', [])]
        if receipt is None or len(found) != 1:
            raise InterventionRequired('Успешная версия истории не найдена')
        self.owned(found[0], release, receipt)
        return receipt

    def save(self, previous_labels, releases):
        desired = {k: v for k, v in previous_labels.items() if not k.startswith(PREFIX)}
        desired.update({PREFIX + 'schema': '1', **dict(zip(KEYS, releases))})
        if desired == previous_labels:
            return
        if self.labels() != previous_labels:
            raise InterventionRequired('Метки изменились параллельно; останови конкурирующий deploy')
        try:
            self.cloud.wait(self.cloud.json(self.function, {'updateMask': 'labels', 'labels': desired}, method='PATCH'))
        except Exception:
            # Потерянный ответ после успешного PATCH не требует повторной мутации.
            if self.labels() != desired:
                raise
        if self.labels() != desired:
            raise InterventionRequired('История установки не подтверждена')

    def plan(self, candidate):
        tag(candidate)
        labels, versions = self.labels(), self.versions()
        current, gateway_digest = self.installed()
        releases = history(labels)
        if releases is None:
            releases = list(LEGACY[self.target.NAME])
            if current != releases[0]:
                raise InterventionRequired('Начальное состояние отличается от согласованной истории')
        for release in releases:
            self.confirmed(release, versions)
        if current is not None and current != releases[0]:
            receipt = self.receipt(current)
            if receipt is not None:
                self.confirmed(current, versions)
                if receipt['gateway_sha256'] != gateway_digest:
                    raise InterventionRequired('Текущий Gateway отличается от квитанции')
                releases = window(current, releases)
        elif current is not None and self.receipt(current)['gateway_sha256'] != gateway_digest:
            raise InterventionRequired('Текущий Gateway отличается от квитанции')
        # Удалённый тёплый тег нельзя воссоздать под старой неизменяемой квитанцией.
        if self.receipt(candidate) is not None and not any(tag(candidate) in v.get('tags', []) for v in versions):
            raise InterventionRequired('Выпуск вне окна отката: опубликуй новый проверенный выпуск из архива')
        protected = {tag(r) for r in releases + [candidate] + ([current] if current else [])}
        remove = []
        for version in versions:
            for name in version.get('tags', []):
                if not re.fullmatch(r'r-[a-f0-9]{32}', name) or name in protected:
                    continue
                env = version.get('environment', {})
                if env.get('GYM_ENVIRONMENT') != self.target.NAME:
                    continue  # Чужие/операторские теги не принадлежат этой политике.
                release = env.get('GYM_RELEASE_ID', '')
                if tag(release) != name:
                    raise InterventionRequired('Не совпала принадлежность управляемого тега')
                self.owned(version, release, self.receipt(release))
                remove.append({'version': version['id'], 'tag': name, 'release': release})
        count = sum(name != '$latest' for v in versions for name in v.get('tags', []))
        candidate_exists = any(tag(candidate) in v.get('tags', []) for v in versions)
        if count - len(remove) + (not candidate_exists) > 10:
            raise InterventionRequired('Недостаточно мест из-за защищённых/неизвестных тегов; они не удалены')
        return {'labels': labels, 'history': releases, 'current': current, 'gateway': gateway_digest,
                'remove': remove, 'candidate': candidate}

    def clean(self, plan):
        expected_labels = self.labels()
        if history(expected_labels) != plan['history']:
            raise InterventionRequired('История изменилась перед очисткой')
        for item in plan['remove']:
            if self.labels() != expected_labels or self.installed() != (plan['current'], plan['gateway']):
                raise InterventionRequired('Состояние изменилось перед очисткой; дальнейшие теги сохранены')
            found = [v for v in self.versions() if item['tag'] in v.get('tags', [])]
            if len(found) != 1 or found[0]['id'] != item['version']:
                raise InterventionRequired('Тег перемещён параллельно; очистка остановлена')
            self.cloud.wait(self.cloud.json(FUNCTIONS + 'versions/' + item['version'] + ':removeTag', {'tag': item['tag']}))
            if any(item['tag'] in v.get('tags', []) for v in self.versions()):
                raise InterventionRequired('Удаление тега не подтверждено')
            print('Function tag retention: removed ' + item['tag'], flush=True)

    def prepare(self, candidate):
        plan = self.plan(candidate)  # Все архивы/квитанции проверены до первой мутации.
        self.save(plan['labels'], plan['history'])
        self.clean(plan)

    def finish(self, receipt):
        release = receipt['release_id']
        if self.installed() != (release, receipt['gateway_sha256']) or self.receipt(release) != receipt:
            raise InterventionRequired('Успешная установка не подтверждена перед ротацией')
        self.confirmed(release, self.versions())
        labels = self.labels()
        previous = history(labels)
        if previous is None:
            raise InterventionRequired('Не инициализирована история установки')
        self.save(labels, window(release, previous))
        self.clean(self.plan(release))


def main():
    import argparse
    import json
    from pathlib import Path
    from infra import production_target, staging_target
    from infra.staging_app import AppCloud
    from infra.ydb_access import Yc
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=('staging', 'production'), required=True)
    parser.add_argument('--yc', type=Path, default=Path('yc'))
    parser.add_argument('--apply', action='store_true', help='Примени только обслуживание тегов текущего выпуска')
    args = parser.parse_args()
    target = staging_target if args.environment == 'staging' else production_target
    token = Yc(args.yc).token()
    retention = TagRetention(AppCloud(token, target=target), token, target)
    current, _ = retention.installed()
    if current is None:
        raise InterventionRequired('Нет установленного выпуска для обслуживания')
    plan = retention.plan(current)
    print(json.dumps({'environment': target.NAME, 'current': current, 'keep': plan['history'],
                      'remove': plan['remove'], 'applied': False}), flush=True)
    if args.apply:
        retention.prepare(current)
        confirmed = retention.plan(current)
        if confirmed['remove']:
            raise InterventionRequired('Очистка осталась незавершённой')
        print(json.dumps({'environment': target.NAME, 'keep': confirmed['history'], 'applied': True}), flush=True)


if __name__ == '__main__':
    main()
