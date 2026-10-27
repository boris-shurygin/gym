# Локальный каркас Gym API

Обработчик `api.handler.handler(event, context)` принимает HTTP-событие Cloud Functions и возвращает `statusCode`, `headers`, `body`, `isBase64Encoded`. Локальный сервер вызывает тот же обработчик. В P03.2.2 проверены runtime `python312` и готовые Linux-зависимости; интеграция хранения и миграции описаны в [db/README.md](../db/README.md).

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

В Linux используй `.local/api-venv/bin/python`. Общие зависимости берутся из [контрактов](../contracts/README.md), runtime-зависимости YDB/SQLAlchemy закреплены в `runtime-requirements.txt`. CI выполняет отдельные проверки API, готового пакета и одноразовой YDB без облачного доступа. HTTP-тесты используют настоящий локальный сервер на случайном порту, строгий JSON и схемы ошибок.

## Поведение этого среза

- `GET /api/v1/health` возвращает 200. Остальные методы этого служебного маршрута и неизвестные пути возвращают `not_found`.
- Личные маршруты берутся из [bindings.json](../contracts/bindings.json) и возвращают `session_required`. Cookie, Bearer, присланные owner ID и claims контекста не создают пользователя и не открывают доступ. Настоящие сессии, CSRF и авторизация прикладных сервисов появятся в P06.
- Публичные маршруты входа и зарезервированные OAuth/MCP возвращают `unavailable`. Это явная заглушка доступности, не реализованный OAuth/MCP-протокол.
- Максимум тела — 1 MiB после base64/UTF-8; JSON-тела публичных входов проходят строгий разбор Gym v1. Для личных маршрутов отказ аутентификации предшествует разбору DTO. Повторяющиеся чувствительные заголовки или неоднозначные представления Gateway отклоняются.
- Все ответы содержат `Cache-Control: no-store` и серверный trace ID. Журнал содержит только метод, код ответа и trace ID; URL, тела, credentials и текст внутренних исключений в него не попадают. Неожиданная ошибка даёт очищенный `unavailable` без квитанции.

Облачная выкатка, вход Яндекс ID, прикладные операции и MCP пока не реализованы. Фабрика подключения/служебные миграции проверены отдельно; HTTP-обработчик не подключается к БД и не исполняет DDL. Демо-данные/очередь `web/` остаются в локальном режиме. Права runtime/запрет DDL и поведение приложения за Gateway проверяются в P04.

## Готовый пакет функции

На Linux x86_64 с Python 3.12:

```bash
python scripts/build_api.py --smoke
python -m unittest discover -s tests/build -p 'test_*.py' -v
```

Сборщик выбирает готовые Linux wheels с ABI cp312/abi3 и glibc не новее 2.28, складывает зависимости в ZIP с `index.handler` и проверяет импорт из распакованного архива изолированным Python. Windows может собрать проверочный кандидат с `--allow-cross-build`, но локально загрузить Linux-библиотеки не может; доверенная сборка выпуска остаётся на Linux в CI.

ZIP не содержит корневого `requirements.txt`, venv, локальных секретов, кэшей или административных миграций. Cloud Functions не должна повторно устанавливать зависимости. Список файлов/время ZIP фиксированы; RECORD зависимостей очищается от не поставляемых CLI-обёрток с временным shebang. Рядом сохраняется отдельный манифест хеша/размеров/runtime/исходников/сборщика/закреплённых требований. Чужое содержимое по уже существующему хешу не перезаписывается. Это метаданные пакета, не доверенный манифест выпуска P04.

Приватная `api.runtime_probe.handler` проверяет импорт нативных grpcio/greenlet/rpds, SDK и зависимости metadata credentials. Она не является маршрутом API или readiness БД; создаёт провайдер credentials без запроса токена/БД. В P04.2.2 добавлен требуемый SDK пакет `requests` с закреплёнными certifi/charset-normalizer/urllib3 и повторена проверка нового ZIP в настоящем `python312` Cloud Functions, Python 3.12.0/glibc 2.35. Текущий пакет — 17 911 217 байт (60 230 739 распакованным), версия `d4ei2qps8avmffr8qm10`, тег `p04-worker-validation`, без SA и доступа к данным. ZIP больше inline-лимита CLI, поэтому передаётся через закрытый Object Storage по SHA-256. Это `cross-wheel-validation`, не доверенный выпуск main. Лимиты упаковки сверяются с [ограничениями провайдера](https://yandex.cloud/ru/docs/functions/concepts/limits). При обновлении runtime/нативных библиотек повтори эту проверку.

Форма события/ответа следует [описанию Python Cloud Functions](https://yandex.cloud/ru/docs/functions/lang/python/handler) и [интеграции Gateway](https://yandex.cloud/ru/docs/api-gateway/concepts/extensions/cloud-functions). При облачной приёмке проверь передачу заголовков и cookie отдельно.
