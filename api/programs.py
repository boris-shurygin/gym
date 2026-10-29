"""Общие прикладные операции программ; REST/MCP не обходят эти проверки."""

from uuid import uuid4
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from contracts.canonical import fingerprint
from contracts.validation import validate
from api.auth import timestamp
from api.errors import ServiceError
from api.sync_state import append_event

RULES = {route['operation_id']: route for route in json.loads(
    (Path(__file__).resolve().parents[1] / 'contracts/bindings.json').read_text(encoding='utf-8'))['routes']}


class Programs:
    def __init__(self, auth):
        self.auth, self.store = auth, auth.store

    def execute(self, operation, token, value):
        rule = RULES.get(operation)
        if not rule:
            raise ServiceError('unavailable')
        # Прямой прикладной вызов обязан соблюдать тот же DTO, что и REST.
        validate(rule['input'], value)
        def perform(db):
            principal = self.auth.principal(db, token)
            owner = principal['user_id']
            if value.get('expected_owner_id', owner) != owner:
                raise ServiceError('account_mismatch')
            if operation == 'get_program':
                head = db.get('program_records', owner, 'head')
                version = db.get('program_records', owner, 'version:' + head['id']) if head else None
                return {'owner_id': owner, 'version': version}
            if operation == 'get_profile':
                profile = db.get('program_records', owner, 'profile') or {'analytics_timezone': 'UTC'}
                return {'user_id': owner, **profile}
            if operation == 'get_change':
                return self._change(db, owner, value['change_id'])
            # Все request_id глобальны в аккаунте: иной endpoint/тело не
            # становится допустимым повтором той же команды.
            request_id = value.get('request_id')
            request_digest = fingerprint('request', {'operation': operation, 'input': value})
            if request_id:
                saved = db.get('program_records', owner, 'request:' + request_id)
                if saved:
                    if saved['digest'] != request_digest:
                        raise ServiceError('request_id_reused')
                    return saved['result']
            now = int(self.auth.now())
            if operation == 'set_profile':
                try:
                    ZoneInfo(value['analytics_timezone'])
                except (ZoneInfoNotFoundError, ValueError):
                    raise ServiceError('validation_failed') from None
                db.put('program_records', owner, 'profile', {'analytics_timezone': value['analytics_timezone']})
                result = {'user_id': owner, 'analytics_timezone': value['analytics_timezone']}
            elif operation == 'propose_change':
                validate('Proposal', value['proposal'])
                result = {'change_id': str(uuid4()), 'owner_id': owner, 'change_revision': 1,
                    'proposal': value['proposal'], 'content_digest': fingerprint('proposal', value['proposal']),
                    'status': 'draft', 'created_at': timestamp(now), 'creator_grant_id': None}
                db.put('program_records', owner, 'change:' + result['change_id'], result)
            else:
                change = self._change(db, owner, value['change_id'])
                if value.get('expected_revision') != change['change_revision']:
                    raise ServiceError('revision_conflict')
                if operation == 'replace_change':
                    if change['status'] == 'published':
                        raise ServiceError('program_conflict')
                    validate('Proposal', value['proposal'])
                    change.update(proposal=value['proposal'], change_revision=change['change_revision'] + 1,
                        content_digest=fingerprint('proposal', value['proposal']), status='draft')
                    db.put('program_records', owner, 'change:' + change['change_id'], change)
                    result = change
                elif operation == 'validate_change':
                    validate('Proposal', change['proposal'])
                    if change['status'] != 'published':
                        self._base(db, owner, change)
                        change['status'] = 'validated'
                        db.put('program_records', owner, 'change:' + change['change_id'], change)
                    result = {'change_id': change['change_id'], 'change_revision': change['change_revision'],
                        'content_digest': change['content_digest'], 'valid': True, 'issues': [],
                        'validated_at': timestamp(now)}
                elif operation in {'approve_and_publish', 'publish_approved'}:
                    result = self._publish(db, owner, principal, operation, value, change, now)
                else:
                    raise ServiceError('unavailable')
            if request_id:
                db.put('program_records', owner, 'request:' + request_id,
                    {'digest': request_digest, 'result': result})
            return result
        def checked(db):
            result = perform(db)
            validate(rule['output'], result)
            return result
        return self.store.run(checked)

    @staticmethod
    def _change(db, owner, change_id):
        change = db.get('program_records', owner, 'change:' + change_id)
        if not change:
            raise ServiceError('not_found')
        return change

    @staticmethod
    def _base(db, owner, change):
        head = db.get('program_records', owner, 'head')
        current = head['id'] if head else None
        if current != change['proposal']['base_program_version_id']:
            raise ServiceError('program_conflict')
        return head

    def _publish(self, db, owner, principal, operation, value, change, now):
        if (value['base_program_version_id'] != change['proposal']['base_program_version_id']
                or value['content_digest'] != change['content_digest']):
            raise ServiceError('approval_invalid')
        previous = db.get('program_records', owner, 'publication:' + change['change_id'])
        if previous:
            if operation == 'publish_approved' and value['approval_id'] != previous['approval_id']:
                raise ServiceError('approval_invalid')
            return previous
        if operation == 'publish_approved':
            # В P06 одобрение и применение атомарны по кнопке Gym. Отдельной
            # выдачи одобрения агенту здесь нет; её grant-проверки — P10.2.
            raise ServiceError('approval_required')
        if now - principal['auth_at'] >= 600:
            raise ServiceError('session_required')
        if change['status'] != 'validated':
            raise ServiceError('validation_failed')
        head = self._base(db, owner, change)
        validate('ProgramContent', change['proposal']['content'])
        version = {'program_id': head['program_id'] if head else 'p_' + uuid4().hex,
            'program_version_id': 'pv_' + uuid4().hex, 'owner_id': owner,
            'version': head['version'] + 1 if head else 1, 'content': change['proposal']['content'],
            'published_at': timestamp(now), 'change_id': change['change_id']}
        result = {'change_id': change['change_id'], 'change_revision': change['change_revision'],
            'approval_id': 'ap_' + uuid4().hex, 'content_digest': change['content_digest'],
            'program_version': version, 'published_at': timestamp(now)}
        # Неизменяемая версия, факт одобрения и текущий указатель фиксируются
        # вместе. SERIALIZABLE не даст двум публикациям заменить одну базу.
        db.put('program_records', owner, 'version:' + version['program_version_id'], version)
        db.put('program_records', owner, 'publication:' + change['change_id'], result)
        db.put('program_records', owner, 'head', {'id': version['program_version_id'],
            'version': version['version'], 'program_id': version['program_id']})
        change['status'] = 'published'
        db.put('program_records', owner, 'change:' + change['change_id'], change)
        append_event(db, owner, {'kind': 'program_published', 'version_id': version['program_version_id']}, now)
        return result
