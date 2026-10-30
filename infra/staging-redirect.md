# Читаемая ссылка staging

Постоянная точка входа: https://gym-staging.website.yandexcloud.net/.
Отдельный пустой бакет `gym-staging` (`e3esd8n8sua77la1d555`) находится
в каталоге Gym staging `b1ghdeobqrevsim6jg9o`. Максимальный размер — 1 MiB.
Публичные чтение, список объектов и конфигурация закрыты; IAM не расширялся.
Не загружай в этот бакет файлы приложения или пользовательские данные.

[staging-redirect.json](staging-redirect.json) задаёт переадресацию всех запросов
на HTTPS-домен существующего Gateway. Запросы `/` и `/app` проверены 2026-10-31:
301, Location сохраняет путь, сертификат проверяется штатным TLS-клиентом.
После перехода адресная строка содержит домен Gateway. Cookies, IndexedDB,
service worker и OAuth callback остаются на прежнем origin.

Первоначальное создание выполнено оператором, вне конвейера выпуска:

```powershell
& ./.local/bin/yc.exe storage bucket create --name gym-staging `
  --folder-id b1ghdeobqrevsim6jg9o --max-size 1048576 `
  --tags project=gym,purpose=staging-shortcut
```

Перед обновлением проверь через `storage bucket get --full` имя, каталог,
resource_id и теги. Не перенастраивай одноимённый чужой ресурс при коллизии.
Применение сохранённой конфигурации:

```powershell
& ./.local/bin/yc.exe storage bucket update --name gym-staging `
  --folder-id b1ghdeobqrevsim6jg9o `
  --website-settings-from-file infra/staging-redirect.json
curl.exe --head https://gym-staging.website.yandexcloud.net/
curl.exe --head https://gym-staging.website.yandexcloud.net/app
```

Ссылка не зависит от релизов. Смена самого Gateway потребует обновления
hostname в конфигурации и проверки Location. Существующий сайт `sportstats`,
закрытые бакеты приложения/артефактов и production не изменялись.
Расходы на запросы/трафик входят в бюджет staging; зарезервированных мощностей нет.

[Документация YC](https://yandex.cloud/ru/docs/storage/operations/hosting/setup).
