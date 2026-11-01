# Первый production с аккаунтами и тренировками

Объём разрешён пользователем 2026-11-01: выпустить проверенную staging-версию в production
и создать читаемую ссылку, если имя свободно. Бакет `gym` уже занят
(`e3e92ptu3j66185h2c85`, другой каталог); не изменяй его. Пользователь выбрал `gym-stats`.
Пустой бакет `gym-stats` создан: `e3erk4hqmc57npsgo7qg`, только production-каталог,
лимит 1 MiB, чтение/список/config_read закрыты, теги project=gym,purpose=production-shortcut.
После успешного выпуска
примени [production-redirect.json](production-redirect.json) и проверь HTTPS 301/Location.
Сайт приложения остаётся на Gateway; в бакет ссылки не загружай файлы или личные данные.
Production origin: `https://d5dhqvda3eo6r4c2mgf6.764nr5vy.apigw.yandexcloud.net`.

## Кандидат и схема

Кандидат: `490995f78087ded09e15273621b56181bb092909_41310675032_1`,
manifest SHA-256 `1b519e26e178a9f1d95c9ad6283d79b0aeb4c023c5233dc813e01f583b120486`.
Успешный полный staging и хеши 10 артефактов проверены; приёмка P11 — в журнале.
На начало подготовки production был на P05 (`0001_runtime_metadata`), кандидат использует
`0003_workouts_sync`. Выпуск завершён 2026-11-01: prepare 41328688017 и promote 41332213364
успешны, публичный release ID кандидата совпал. Ссылка https://gym-stats.website.yandexcloud.net/
работает: HTTPS 301 с сохранением пути на production Gateway, чтение/список/config_read бакета закрыты.

Используй только защищённый workflow `Production release` из main. Операция `prepare`
разрешает известный additive-переход 0001 → 0003 при прежней совместимости Gym v1.
Миграции выполняются приватным облачным исполнителем; API/PWA/Gateway не переключаются.
Успех подготовки — `state=schema_prepared`, а не успешный выпуск приложения.
Новая операция не создаёт deployment receipt приложения и не даёт CI прямого доступа к YDB.

После успешного prepare примени точные права ниже, затем выполни `promote` того же release ID.
Готовый migration job переиспользуется. Прямой promote через несовпадение схем, неизвестные
ревизии, незавершённое чужое задание и downgrade запрещены. Rollback требует той же схемы
и ранее успешного deployment в этом окружении: P05 после перехода к 0003 не является
допустимой целью rollback. Архив P05 сохраняется, схема не откатывается автоматически.

## Отдельный OAuth

Production-приложение Яндекс ID `5fc1d13a73e24328ab4e603f9f288636`: только `login:info`, callback `/auth/callback/yandex`
на указанном production origin. Метаданные хранятся в `production-auth.json`;
staging-конфигурация не наследуется. Promote 0003 с выключенным OAuth останавливается до
первой записи намерения/изменения ресурсов. Секрет вводит пользователь локально:

```powershell
& ./scripts/Set-GymProductionOAuth.ps1 -ClientId <client-id>
```

Скрипт проверяет отсутствие дубликата, использует скрытый ввод и передаёт значение прямо
в Lockbox `gym-production-yandex-id`, каталог `b1gt4o1311mhe57522j8`, с защитой удаления.
Созданный секрет — `e6qotfhl6b475l4qi4qs`, версия `e6q3s9rbt51r1kvj9034`; статус ACTIVE,
единственный payload-ключ `client_secret`. Конфигурация ссылается на эту точную версию.
В Git/выводе только Client ID и ID секрета/версии. При неопределённом результате сначала
перечитай метаданные операции/ресурса; не создавай второй секрет вслепую.

## Точные новые права

Только SA API production `aje8tgptbcm31migrtnj`, только YDB `etn2tjdah1oj7i9055ko`
и отдельный production-секрет OAuth:

| Ресурс | Права |
| --- | --- |
| `gym_admissions` | `ydb.granular.describe_schema`, `ydb.granular.select_row` |
| `gym_login_transactions`, `gym_sessions`, `gym_login_requests`, `gym_program_records`, `gym_workout_records` | `ydb.granular.describe_schema`, `ydb.granular.select_row`, `ydb.granular.update_row`, `ydb.granular.erase_row` |
| Только `gym-production-yandex-id` | `lockbox.payloadViewer` |

Корень базы, административные таблицы, DDL/GRANT для API и прямой доступ CI/Gateway
к данным не выдаются. Прежние IAM/DDL-права мигратора не расширяются.
Monium production не подключается: экспорт P11 настроен только в staging.

```powershell
& ./.local/backend-venv/Scripts/python.exe -m infra.production_runtime_access
& ./.local/backend-venv/Scripts/python.exe -m infra.production_runtime_access --apply
```

План проверяет окружение/секрет, существующие ACL и изоляцию CI/Gateway; неожиданные права
останавливают его до мутаций. До prepare план показывает будущие таблицы, но применение
запрещено. Применение выполняется оператором после успешного prepare, вне CI.
Перечитай права и проверь запрет чтения служебных таблиц от имени SA API; личные строки не выводи.
Применение выполнено 2026-11-01 после успешного prepare 41328688017. Прикладное чтение
подтверждено; запрос к gym_migration_jobs получил явный AccessDenied (Aborted, issue 2028).
Не считай обычный тайм-аут доказательством запрета. Права корня/DDL и прямой доступ CI не добавлялись.

## Допуск владельца

Не копируй аккаунты, программы или тренировки из staging. Используй reference подтверждённого
входа конкретного пользователя именно в production:

```powershell
& ./.local/backend-venv/Scripts/python.exe -m infra.accounts_admin --yc .local/bin/yc.exe --environment production allow --reference <trace-id>
```

Ежедневные копии production уже настроены на 17:00 UTC, TTL 7 дней; лимиты базы
10 RU/с и 1 GiB, защита удаления включена. Второе расписание не создавай.
