"""Серверное согласие OAuth не зависит от кэшированной оболочки PWA."""
from html import escape
import hmac
from urllib.parse import parse_qsl

from api.auth import SESSION_COOKIE, AGENT_COOKIE, cookies, cookie, digest
from api.delegation import Delegation
from api.errors import ServiceError

LABELS = {'program:read': 'Читать программу', 'history:read': 'Читать историю тренировок',
    'stats:read': 'Читать статистику', 'program:draft': 'Готовить и проверять изменения программы',
    'program:publish': 'Подтверждать публикацию после твоего одобрения в Gym'}


def handle(request, auth, trace):
    from api.handler import _response
    jar = cookies(request.headers.get('cookie', ''))
    token, identifier = jar.get(SESSION_COOKIE), jar.get(AGENT_COOKIE)
    if not identifier or request.query or request.method not in ('GET', 'POST'):
        raise ServiceError('invalid_request')
    def redirect(location):
        response = _response(302, None, trace)
        response['body'], response['headers']['Location'] = '', location
        return response
    try:
        principal = auth.store.run(lambda db: auth.principal(db, token))
    except ServiceError as error:
        if error.code == 'session_required' and request.method == 'GET':
            return redirect('/auth/login?return_to=/agent')
        raise
    if int(auth.now()) - principal['auth_at'] >= 600:
        return redirect('/auth/login?purpose=reauth&return_to=/agent')
    delegation = Delegation(auth)
    if request.method == 'POST':
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise ServiceError('invalid_request')
        try:
            pairs = parse_qsl(request.body.decode('utf-8'), strict_parsing=True, max_num_fields=4)
        except (ValueError, UnicodeError):
            raise ServiceError('invalid_request') from None
        value = dict(pairs)
        if (len(value) != len(pairs) or set(value) != {'request_id', 'allow', 'csrf_token'}
                or not hmac.compare_digest(value['request_id'].encode(), identifier.encode())
                or value['allow'] not in ('yes', 'no')):
            raise ServiceError('invalid_request')
        auth.check_write(token, {**request.headers, 'x-csrf-token': value['csrf_token']})
        result = delegation.consent(token, {'request_id': identifier, 'allow': value['allow'] == 'yes'}, True)
        response = redirect(result['redirect_uri'])
        response['multiValueHeaders'] = {'Set-Cookie': [cookie(AGENT_COOKIE)]}
        return response
    if request.body:
        raise ServiceError('invalid_request')
    info = delegation.consent(token, {'request_id': identifier})
    scopes = ''.join('<li>' + escape(LABELS[scope]) + '</li>' for scope in info['scopes'])
    body = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Доступ агента — Gym</title><style>body{{font:16px system-ui;background:#f5f6fa;color:#202b40;margin:0;padding:24px}}
main{{max-width:620px;margin:6vh auto;background:white;border:1px solid #e9ecf3;border-radius:24px;padding:28px}}
h1{{font-size:28px}}p,li{{line-height:1.6}}button{{font:inherit;border:1px solid #dde3f2;border-radius:12px;padding:14px 20px;margin:8px 8px 0 0;cursor:pointer}}
button[value=yes]{{background:#3e63ed;color:white;border-color:#3e63ed}}small{{color:#778196}}a{{color:#3553dc}}</style>
<main><small>Gym · отдельное разрешение</small><h1>Подключить агента к Gym</h1>
<p>Аккаунт: <strong>{escape(principal.get('yandex_login') or 'Твой аккаунт Gym')}</strong></p>
<h2>{escape(info['client_name'])} запрашивает:</h2><ul>{scopes}</ul>
<p>Доступ можно отозвать в Gym. Срок — до 90 дней, без использования — до 30 дней.
Агент не сможет исправлять факты тренировок. Каждую новую программу ты отдельно одобряешь в Gym.</p>
<form method="post" action="/oauth/consent"><input type="hidden" name="request_id" value="{escape(identifier)}">
<input type="hidden" name="csrf_token" value="{digest('gym:csrf:' + token)}">
<button name="allow" value="yes" type="submit">Разрешить доступ</button><button name="allow" value="no" type="submit">Отказать</button></form>
<p><a href="/auth/login?purpose=switch&amp;return_to=/agent">Войти другим аккаунтом</a></p></main></html>'''
    response = _response(200, None, trace)
    response['headers'].update({'Content-Type': 'text/html; charset=utf-8', 'X-Frame-Options': 'DENY',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self' http://127.0.0.1:8765/callback https://chatgpt.com/connector_platform_oauth_redirect"})
    response['body'] = body
    return response
