"""Stateless Streamable HTTP / JSON-RPC: общие DTO и прикладные сервисы."""

import json
from pathlib import Path
import re
from urllib.parse import parse_qsl

from jsonschema import ValidationError
from contracts.canonical import parse_request
from contracts.validation import validate
from api.analytics import Analytics, OPERATIONS as ANALYTICS
from api.auth import SESSION_COOKIE, cookies
from api.delegation import Delegation, DelegatedAuth, OAuthError
from api.errors import ServiceError
from api.programs import Programs, RULES
from api.workouts import Workouts

TOOLS = json.loads((Path(__file__).resolve().parents[1] / 'contracts/mcp-tools.json').read_text(encoding='utf-8'))['tools']
BY_NAME = {tool['name']: tool for tool in TOOLS}
VERSIONS = ('2025-03-26', '2025-06-18', '2025-11-25')


def handle(request, auth, trace):
    from api.handler import _response, _error
    if auth is None:
        raise ServiceError('unavailable')
    delegation = Delegation(auth)
    path = request.path
    token = cookies(request.headers.get('cookie', '')).get(SESSION_COOKIE) if path == '/api/v1/oauth/consent' else None
    try:
        if path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp',
                '/.well-known/oauth-authorization-server') and request.method == 'GET':
            if request.query or request.body:
                raise OAuthError()
            return _response(200, delegation.metadata('oauth-protected-resource' in path), trace)
        if path == '/oauth/authorize' and request.method == 'GET':
            if request.body:
                raise OAuthError()
            response = _response(302, None, trace)
            response['body'] = ''
            response['headers']['Location'] = delegation.begin(request.query)
            return response
        if path in ('/oauth/token', '/oauth/revoke') and request.method == 'POST':
            if request.query or request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                raise OAuthError()
            try:
                pairs = parse_qsl(request.body.decode('utf-8'), strict_parsing=True, max_num_fields=12)
            except (UnicodeError, ValueError):
                raise OAuthError() from None
            value = dict(pairs)
            if len(value) != len(pairs):
                raise OAuthError()
            result = delegation.token(value) if path.endswith('/token') else delegation.revoke_token(value)
            response = _response(200, result or {}, trace)
            if result is None:
                response['body'] = ''
            return response
        if path == '/api/v1/oauth/consent' and request.method in ('GET', 'POST'):
            identity = auth.session(token)
            if request.headers.get('x-gym-expected-owner', identity['user_id']) != identity['user_id']:
                raise ServiceError('account_mismatch')
            if request.method == 'POST':
                auth.check_write(token, request.headers)
                request.check_json()
                if request.query:
                    raise OAuthError()
                value = parse_request(request.body)
            else:
                if request.body:
                    raise OAuthError()
                value = request.query
            return _response(200, delegation.consent(token, value, request.method == 'POST'), trace)
    except OAuthError as error:
        return _response(400, {'error': error.code}, trace)
    if path != '/mcp':
        raise ServiceError('not_found')
    if request.headers.get('origin') not in (None, delegation.origin):
        return _response(403, {'error': 'origin_not_allowed'}, trace)
    header = request.headers.get('authorization', '')
    bearer = re.fullmatch(r'Bearer ([A-Za-z0-9_-]{43,128})', header, re.IGNORECASE)
    access = bearer[1] if bearer else None
    try:
        principal = auth.store.run(lambda db: delegation.principal(db, access))
    except ServiceError as error:
        response = _error(error.code, trace)
        response['headers']['WWW-Authenticate'] = (f'Bearer resource_metadata="{delegation.origin}/.well-known/oauth-protected-resource/mcp", '
            'error="invalid_token"')
        return response
    if request.method != 'POST':
        response = _response(405, {'error': 'method_not_allowed'}, trace)
        response['headers']['Allow'] = 'POST'
        return response
    if request.query or request.headers.get('mcp-protocol-version', '2025-03-26') not in VERSIONS:
        raise ServiceError('invalid_request')
    if not all(kind in request.headers.get('accept', '') for kind in ('application/json', 'text/event-stream')):
        return _response(406, {'error': 'unsupported_accept'}, trace)
    request.check_json()
    try:
        message = parse_request(request.body)
    except ValueError:
        return _response(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}, trace)
    if (not isinstance(message, dict) or message.get('jsonrpc') != '2.0'
            or set(message) - {'jsonrpc', 'id', 'method', 'params'}
            or not isinstance(message.get('method'), str)
            or not isinstance(message.get('params', {}), dict)
            or 'id' in message and (type(message['id']) not in (str, int) or len(str(message['id'])) > 128)):
        return _response(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}, trace)
    method, params, identifier = message['method'], message.get('params', {}), message.get('id')
    if 'id' not in message:
        response = _response(202, None, trace)
        response['body'] = ''
        return response
    def rpc(result=None, error=None):
        return _response(200, {'jsonrpc': '2.0', 'id': identifier,
            **({'error': error} if error else {'result': result})}, trace)
    if method == 'initialize':
        return rpc({'protocolVersion': params.get('protocolVersion') if params.get('protocolVersion') in VERSIONS else VERSIONS[-1],
            'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': {'name': 'gym', 'version': '1.0.0'},
            'instructions': 'Читай факты через инструменты. Публикуй программу только после одобрения в Gym.'})
    if method == 'ping':
        return rpc({})
    if method == 'tools/list':
        return rpc({'tools': [tool | {'securitySchemes': [{'type': 'oauth2', 'scopes': [tool['_meta']['gym.requiredScope']]}]}
            for tool in TOOLS if tool['_meta']['gym.requiredScope'] in principal['scopes']]})
    if method != 'tools/call':
        return rpc(error={'code': -32601, 'message': 'Method not found'})
    tool = BY_NAME.get(params.get('name')) if isinstance(params.get('name'), str) else None
    if not tool or set(params) - {'name', 'arguments', '_meta'}:
        return rpc(error={'code': -32602, 'message': 'Unknown tool or invalid parameters'})
    operation, scope = tool['_meta']['gym.operationId'], tool['_meta']['gym.requiredScope']
    try:
        delegated = DelegatedAuth(delegation, scope)
        if scope not in principal['scopes']:
            raise ServiceError('insufficient_scope')
        value = params.get('arguments', {})
        validate(RULES[operation]['input'], value)
        service = Programs(delegated) if operation in {'get_program', 'propose_change', 'get_change',
            'replace_change', 'validate_change', 'publish_approved'} else Analytics(delegated) if operation in ANALYTICS else Workouts(delegated)
        data = service.execute(operation, access, value)
        validate(RULES[operation]['output'], data)
        structured = {'ok': True, 'data': data}
        meta = {}
        text = json.dumps(structured, ensure_ascii=False, separators=(',', ':'))
        if operation in {'propose_change', 'replace_change', 'get_change'}:
            meta['gym/review_url'] = delegation.origin + '/#/program?change_id=' + data['change_id']
            text += '\nПроверь и одобри в Gym: ' + delegation.origin + '/#/program?change_id=' + data['change_id']
            if data['status'] == 'published' and data['creator_grant_id'] == principal['grant_id'] and 'program:publish' in principal['scopes']:
                def publication(db):
                    current = delegation.principal(db, access, 'program:publish')
                    return db.get('program_records', current['user_id'], 'publication:' + data['change_id'])
                saved = auth.store.run(publication)
                if saved:
                    meta['gym/publication'] = saved
                    text += '\nУже одобрено и опубликовано в Gym: ' + json.dumps(saved, ensure_ascii=False)
        return rpc({'content': [{'type': 'text', 'text': text}], 'structuredContent': structured, 'isError': False,
            **({'_meta': meta} if meta else {})})
    except (ValidationError, ValueError):
        error = ServiceError('validation_failed')
    except ServiceError as caught:
        error = caught
    envelope = json.loads(_error(error.code, trace, receipt=error.receipt)['body'])
    structured = {'ok': False, 'error': envelope['error']}
    result = {'content': [{'type': 'text', 'text': json.dumps(structured, ensure_ascii=False)}],
        'structuredContent': structured, 'isError': True}
    if error.code in {'invalid_token', 'insufficient_scope'}:
        response = _error(error.code, trace)
        response['headers']['WWW-Authenticate'] = (f'Bearer resource_metadata="{delegation.origin}/.well-known/oauth-protected-resource/mcp", '
            f'error="{error.code}", scope="{scope}"')
        return response
    return rpc(result)
