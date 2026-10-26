# Локальный каркас Gym API

P03.2.1: обработчик `api.handler.handler(event, context)` принимает HTTP-событие Cloud Functions и возвращает `statusCode`, `headers`, `body`, `isBase64Encoded`. Локальный сервер вызывает тот же обработчик. Python 3.12 используется для локальной разработки/CI; выбор облачного runtime и готовый Linux ZIP проверяются в P03.2.2.

## Запуск и проверки

Из корня репозитория в PowerShell:

```powershell
py -3.12 -m venv .local/api-venv
& ./.local/api-venv/Scripts/python.exe -m pip install -r api/requirements.txt
& ./.local/api-venv/Scripts/python.exe -m api.dev --port 8080
```

Открой <http://127.0.0.1:8080/api/v1/health>. Ответ `{"status":"ok"}` подтверждает работу HTTP-процесса, без проверки БД, миграций или внешнего входа. Сервер слушает только `127.0.0.1`; это локальный адаптер, не способ публикации приложения.

```powershell
& ./.local/api-venv/Scripts/python.exe -m unittest discover -s tests/api -p 'test_*.py' -v
```

В Linux используй `.local/api-venv/bin/python`. Зависимости пока совпадают с [контрактами](../contracts/README.md); YDB/SQLAlchemy добавляются при реальной интеграции. CI выполняет отдельный job `api` без облачного доступа. Тесты используют настоящий локальный HTTP-сервер на случайном порту, строгий JSON и схемы ошибок.

## Поведение этого среза

- `GET /api/v1/health` возвращает 200. Остальные методы этого служебного маршрута и неизвестные пути возвращают `not_found`.
- Личные маршруты берутся из [bindings.json](../contracts/bindings.json) и возвращают `session_required`. Cookie, Bearer, присланные owner ID и claims контекста не создают пользователя и не открывают доступ. Настоящие сессии, CSRF и авторизация прикладных сервисов появятся в P06.
- Публичные маршруты входа и зарезервированные OAuth/MCP возвращают `unavailable`. Это явная заглушка доступности, не реализованный OAuth/MCP-протокол.
- Максимум тела — 1 MiB после base64/UTF-8; JSON-тела публичных входов проходят строгий разбор Gym v1. Для личных маршрутов отказ аутентификации предшествует разбору DTO. Повторяющиеся чувствительные заголовки или неоднозначные представления Gateway отклоняются.
- Все ответы содержат `Cache-Control: no-store` и серверный trace ID. Журнал содержит только метод, код ответа и trace ID; URL, тела, credentials и текст внутренних исключений в него не попадают. Неожиданная ошибка даёт очищенный `unavailable` без квитанции.

Облачная выкатка, YDB/транзакции, миграции, вход Яндекс ID, прикладные операции и MCP пока не реализованы. HTTP-обработчик не подключается к БД и не исполняет DDL. Демо-данные/очередь `web/` остаются в локальном режиме. Реальные семантики YDB, ограничения ZIP и поведение за Gateway ещё нужно проверить в P03.2.2/P04; эти тесты их не заменяют.

Форма события/ответа следует [описанию Python Cloud Functions](https://yandex.cloud/ru/docs/functions/lang/python/handler) и [интеграции Gateway](https://yandex.cloud/ru/docs/api-gateway/concepts/extensions/cloud-functions). При облачной приёмке проверь передачу заголовков и cookie отдельно.
