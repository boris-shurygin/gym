# Восстановление staging job после ограничения YDB

После merge #27 main `a88c9bfeda9d6d219b057fc8039e916b5c4adf97`, [Staging release 41073229675](https://github.com/boris-shurygin/gym/actions/runs/41073229675) остановился на migrations до установки приложения. Durable status: `repair_required`, checkpoint 0. Одновременно чтение собственных синтетических строк возвращало `RESOURCE_EXHAUSTED`; staging ограничен 10 RU/с. Лимит не увеличивается. Собственные временные аккаунты/строки приёмки удалены, браузеры закрыты. Добавляется прекращение периодического опроса скрытыми вкладками.

## Точная граница

- Только staging-контейнер `bba0n53on27kvvqler17`, прежний SA `ajedn74q9psjfecqnteg`, YDB `etn63tldguvkj5kcpi3r`, namespace `gym_`.
- Единственный job `migration_a88c9bfeda9d6d219b057fc8039e916b5c4adf97_41073229675_1`, generation **1**, owner `dfe407c5b2e742229a910732b3e9baa9`, checksum `a54673bf21b2097cc0f0e24a6b5af119c8f235306e9921b6944cec3e4e56bf32`.
- Прежняя ревизия `bbai4f6rj9pcb8pvnno9`, timeout 120 s; образ `cr.yandex/crpe91o8ka9e03n3n90c/gym-migrations@sha256:b943d6a7e1d9edf408e1cd2537817a8a5d5a3c57337ec012114a30314d21164b` из проверенного манифеста.
- Схема `0003_workouts_sync` уже существовала. Не делай downgrade, прямые исправления таблиц/lease, bootstrap или IAM/ACL-изменения. Production не затрагивается.

## Процедура

Следуй [регламенту](migration-worker.md#административное-восстановление). Проверь завершение workflow и точный status. Переключи контейнер на новую обычную ревизию того же образа/выпуска, не вызывая step. Сохрани ID операции до ожидания. Подтверди `OBSOLETE` старой ревизии и выжди **180 секунд от подтверждённого переключения**.

Сохрани evidence/ticket с owner/generation/job, временем и SHA-256. Штатный `migration_container.recovery_evidence` проверяет их, принадлежность старой ревизии, digest, SA, HTTP и timeout. Опубликуй recovery-ревизию этого образа/выпуска с точным ticket, вызови `recover`. Сравнение owner/generation выполняется внутри транзакции worker. Проверь `needs_resume`, верни обычный worker без ticket, возобнови **тот же job** и проверь схему/`succeeded` в новом поколении. При расхождении остановись. Успех старого CI не переписывается: следующий main проходит новый обычный staging workflow.

Свидетельства и квитанции — в игнорируемой `.local/`, без токенов. При потерянном ответе сначала проверь сохранённый ID/реальное состояние, не повторяй POST вслепую.

## Результат

Восстановление выполнено после подтверждённого OBSOLETE и 180 s ожидания. Recovery receipt `1ca969ec85dc06778590cd6dfeb8a9bb2a9094745745fd3d1000fabc54b24328`; затем обычный worker завершил тот же job: `succeeded`, generation **3**, схема `0003_workouts_sync` проверена. IAM, лимит YDB, пользовательские данные и production не менялись. Сбой прежнего CI сохранён как факт; новый main проходит отдельный workflow.
