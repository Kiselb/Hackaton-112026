# CLIP-009 — verified backup

## Граница процедуры

Процедура сохраняет только существующие ресурсы AI-Agent Clip:

| Компонент | Ресурс |
|---|---|
| PostgreSQL | `clip_agent_2026` |
| Qdrant | `clip_agent_dense_v1` |
| OpenSearch | `clip_agent_sparse_v1` |
| Files | фактический `STORAGE_ROOT` из существующего `.env` |

Она не создаёт вторую конфигурацию приложения, не меняет RAG/API/frontend, не выполняет
restore и ничего не удаляет. Скрипт отказывается работать с иными именами ресурсов,
удалёнными endpoint или каталогом backup внутри Git/`STORAGE_ROOT`. `.env`, DSN и пароли
не копируются и не записываются в manifest.

Backup содержит документы и трассы и является приватным. По умолчанию каждый запуск
создаёт новый каталог
`Documents/AI-Agent-Clip-Backups/clip009-YYYYMMDD-HHMMSS/`. В нём создаются `.gitignore`
и `PRIVATE_BACKUP_DO_NOT_PUBLISH.txt`; автоматической ротации или удаления нет.

## Предварительные условия

1. Владелец остановил FastAPI и Caddy либо иным способом закрыл все пути записи. Скрипт
   сам процессы не завершает и блокирует backup при listener на настроенных demo-портах.
2. В PostgreSQL нет документов со статусом `indexing`.
3. Docker-сервисы доступны; PostgreSQL container содержит клиент 17 `pg_dump` и
   `pg_restore`.
4. OpenSearch имеет заранее настроенный `fs` snapshot repository `clip_backup`, чей
   каталог является host bind mount вне Git и Docker volumes.
5. Нет другой активной операции OpenSearch snapshot и достаточно свободного места.

Проверить без создания каталога, dump или snapshot:

```powershell
.\scripts\backup\Backup-Clip.ps1 -Preflight
```

Для отличающихся имён контейнеров/repository передать их явно:

```powershell
.\scripts\backup\Backup-Clip.ps1 -Preflight `
  -PostgresContainer rag-postgres-1 `
  -OpenSearchContainer rag-opensearch-1 `
  -OpenSearchRepository clip_backup
```

Preflight читает `.env`, но не печатает DSN/user/password. Он проверяет точные имена,
таблицы PostgreSQL, отсутствие `indexing`, версии clients, здоровье Qdrant/индекса,
OpenSearch repository и его фактический bind mount, listeners и свободное место.

## Текущий блокер OpenSearch

На проверке 2026-11-24 `GET /_snapshot` вернул `{}`, а node settings не содержали
`path.repo`. Контейнер `rag-opensearch-1` создан сервисом `opensearch` Compose-проекта
`rag`; Compose-файл разрешён через container labels и находится вне AI-Agent Clip:

```text
D:\EDUCATION\AIU Стажировка Водоканал\rvk-office-ai\RAG\docker-compose.yml
```

Диагностика CLIP-009-INFRA подтвердила фактический Docker volume
`rag_opensearch_data` (Compose logical name `opensearch_data`) на
`/usr/share/opensearch/data`. Он также обслуживает прежний проект, поэтому менять или
перезапускать его без отдельного разрешения нельзя. До изменений cluster был `green`,
все 14 индексов были `open/green`, активных snapshot/recovery не было.

Выбранный Windows path получен через `GetFolderPath('MyDocuments')`:

```text
C:\Users\mvkiselev\Documents\AI-Agent-Clip-Backups\opensearch-repository
```

Он находится вне Git и Docker volumes; на диске было свободно около 1.37 TiB. На момент
диагностики каталог ещё не создан, поэтому фактическая Docker write-проверка и ACL
самого каталога не выполнены. Sandbox разрешает только read-only доступ к внешнему
Compose/Documents: изменения должен применить владелец вручную.

Безопасный план, который **ещё не выполнен**:

1. Владелец выбирает физический host-каталог
   `Documents\AI-Agent-Clip-Backups\opensearch-repository` и проверяет его свободное
   место/защиту.
2. В существующий сервис `opensearch` добавить setting
   `path.repo: /mnt/clip-backups` и bind mount выбранного host-каталога на
   `/mnt/clip-backups`. Существующий `opensearch_data` не менять.
3. После явного согласия пересоздать только `opensearch`, без `down`, `-v` и операций с
   другими сервисами/volumes.
4. Проверить OpenSearch 3.8.0, cluster health, все прежние индексы и отдельно
   `clip_agent_sparse_v1`.
5. Зарегистрировать `clip_backup` типа `fs` с `location=/mnt/clip-backups`, выполнить
   `POST /_snapshot/clip_backup/_verify` и повторить preflight.

Планируемая минимальная дельта только внутри существующего `opensearch` service
(дельта пока не применена):

```yaml
environment:
  path.repo: /mnt/clip-backups
volumes:
  - opensearch_data:/usr/share/opensearch/data
  - type: bind
    source: C:/Users/mvkiselev/Documents/AI-Agent-Clip-Backups/opensearch-repository
    target: /mnt/clip-backups
```

И текущий, и in-memory candidate прошли Docker Compose 2.40.2
`config --quiet` с exit code 0. Прямая настройка `path.repo` через environment с точкой
поддерживается Docker image OpenSearch: такие settings преобразуются image entrypoint в
`-E` startup flags. Официальные ссылки:
[configuration](https://docs.opensearch.org/latest/opensearch/configuration/) и
[snapshot/restore](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/).

Отдельный риск recreate: работающий контейнер имеет `Config.Image=...:latest`, image ID
`sha256:bcc179...`, но API сообщает версию 3.8.0. Compose уже содержит точный tag
`...:3.8.0`, которого локально нет. До recreate следует после подтверждения владельца
присвоить **тому же image ID** локальный tag `3.8.0` и запускать с `--pull never`; это
исключает молчаливую смену bits/версии.

### Результат разрешённого recreate

После явного подтверждения владельца 2026-11-24 текущему image ID был назначен локальный
tag `3.8.0`, затем выполнен только `opensearch` с
`--no-deps --force-recreate --pull never`. Новый container ID: `68a440de48fd...`; image
ID и volume `rag_opensearch_data` сохранились. PostgreSQL/Qdrant container IDs не
изменились. OpenSearch 3.8.0 вернулся в `green`, `path.repo=/mnt/clip-backups`, bind
mount виден и доступен для записи.

Дальнейшая процедура остановлена до решения владельца: у всех 14 видимых индексов
сохранились имена, UUID и live `docs.count`, но у
`top_queries-2026.11.23-89315` физический Lucene `docs.deleted` изменился с 6 на 4, а у
`top_queries-2026.11.17-89288` `store.size` изменился с 82.6 kB на 23 kB. `_count`
первого индекса остаётся 322; segment stats показывают 322 live docs и два tombstones в
непоисковом сегменте, active merge/task/recovery отсутствуют. Это соответствует очистке
физических segment files при shard recovery, но согласно stop-rule не считается
автоматически принятым расхождением. `clip_backup` ещё не зарегистрирован, `_verify` и
CLIP-009 `-Preflight` после recreate не запускались.

Владелец отдельно принял эти физические изменения Lucene. После этого `clip_backup`
зарегистрирован как `fs` с `location=/mnt/clip-backups`: PUT вернул
`acknowledged=true`, GET подтвердил точную конфигурацию, `_verify` подтвердил node
`68a440de48fd`.

Windows PowerShell 5.1 `-Preflight` затем остановился до backup на трёх подтверждённых
дефектах portability скрипта:

1. параметр `[string]$Repository` и локальная переменная `$repository` считаются одним
   регистронезависимым именем; repository object приводится к строке, после чего
   `$repository.type` падает на строке 198;
2. Docker Desktop сообщает bind source как
   `/run/desktop/mnt/host/c/Users/...`; `GetFullPath` в Windows превращает его в
   несуществующий `D:\run\desktop\...`, хотя фактический каталог на `C:` существует;
3. `@($mountJson | ConvertFrom-Json)` в Windows PowerShell 5.1 сохраняет JSON array как
   один вложенный `System.Object[]`, поэтому named volume и repository bind не
   обрабатываются как отдельные mounts.

Исправление использует отдельное имя `$repositoryConfig`, детерминированно преобразует
Docker Desktop source вида `/run/desktop/mnt/host/<drive>/...` обратно в Windows path и
явно разворачивает каждый mount. Repository принимается только при единственном точном
совпадении `Destination`, `Type=bind` и существующем host-каталоге; неизвестные форматы
по-прежнему завершаются fail closed. Pytest дополнительно запрещает `--basetemp` внутри
рабочего `STORAGE_ROOT`.

После удаления трёх подтверждённых sandbox test artifacts и read-only инвентаризации
`STORAGE_ROOT` финальный `-Preflight` 2026-11-25 завершился с exit code 0. Проверены
PostgreSQL 17.11, Qdrant 1.15.5, OpenSearch 3.8.0, 17 storage files/37,315 bytes,
отсутствие listeners FastAPI/Caddy и repository `clip_backup` по Windows host path.
Фактический backup не запускался, каталогов `clip009-*` нет; `-Backup` по-прежнему
требует отдельного разрешения.

После согласованного recreate и проверки cluster/index repository регистрируется один
раз:

```powershell
$body = @{
  type = 'fs'
  settings = @{ location = '/mnt/clip-backups' }
} | ConvertTo-Json -Depth 3
Invoke-RestMethod -Method Put -ContentType 'application/json' -Body $body `
  'http://127.0.0.1:9200/_snapshot/clip_backup'
Invoke-RestMethod -Method Post `
  'http://127.0.0.1:9200/_snapshot/clip_backup/_verify'
```

Регистрация repository меняет только метаданные snapshot, но всё равно выполняется
после согласованного изменения Compose. Не указывать host password или `.env` в Compose
дельте/отчёте.

## Создание backup

После успешного preflight и остановки путей записи:

```powershell
.\scripts\backup\Backup-Clip.ps1 -Backup
```

Последовательность:

1. `pg_dump -Fc` только базы `clip_agent_2026`; архив читается через
   `pg_restore --list`, проверяются `documents`, `chunks`, `search_traces` и
   `trace_feedback`.
2. API snapshot только Qdrant collection `clip_agent_dense_v1`; snapshot скачивается и
   повторно находится через API.
3. Все файлы `STORAGE_ROOT` копируются без изменения источника; сравниваются число,
   размер и SHA-256 каждого файла.
4. OpenSearch создаёт snapshot только `clip_agent_sparse_v1` с
   `include_global_state=false`; требуется `SUCCESS` и ровно один ожидаемый индекс.
   После этого весь физический repository копируется в каталог запуска и сверяется по
   SHA-256.
5. Повторно проверяется отсутствие `indexing`, записывается `manifest.json`.

При ошибке скрипт прекращает следующие операции, сохраняет уже созданные артефакты и
manifest с `complete=false` и текстом ошибки. Такой каталог не является полным backup.
Серверные Qdrant/OpenSearch snapshots намеренно не удаляются.

## Manifest и проверка целостности

`manifest.json` содержит UTC timestamp, Git SHA/dirty, ресурсы, версии, проверки
компонентов, ограничения, список файлов, размеры и SHA-256. Абсолютный `STORAGE_ROOT`,
host path repository, DSN и credentials из manifest исключены.

Повторная локальная проверка файлов:

```powershell
$root = 'C:\Users\<owner>\Documents\AI-Agent-Clip-Backups\clip009-YYYYMMDD-HHMMSS'
$manifest = Get-Content (Join-Path $root 'manifest.json') -Raw | ConvertFrom-Json
foreach ($item in $manifest.files) {
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $root $item.path)).Hash
  if ($actual.ToLowerInvariant() -ne $item.sha256) { throw "Hash mismatch: $($item.path)" }
}
if (-not $manifest.complete) { throw 'Backup is incomplete.' }
```

Checksum и `pg_restore --list` обнаруживают часть повреждений, но не доказывают
восстановимость или смысловую согласованность.

## Ограничение согласованности

У четырёх stores нет общей транзакции/snapshot boundary. Остановленные пути записи и
двойная проверка `status=indexing` предотвращают штатные новые ingestion, но не могут
атомарно зафиксировать PostgreSQL, Qdrant, storage и OpenSearch в один момент. Во время
backup нельзя запускать upload, deactivate/restore, ask/feedback или сторонние операции
с индексами/snapshot repository. Последовательная копия считается
application-consistent только при соблюдении этого quiesce.

## Общий план восстановления (не выполнять на рабочих ресурсах)

Restore является отдельной destructive процедурой и требует новых явных имён и согласия:

1. проверить SHA-256 и `complete=true`;
2. создать новую временную PostgreSQL database и выполнить restore custom dump без
   `--clean`;
3. создать новую временную Qdrant collection из скачанного snapshot;
4. развернуть копию OpenSearch repository в отдельный разрешённый repository и
   восстановить snapshot в новый индекс;
5. скопировать storage в новый временный каталог;
6. process-only настройками направить отдельный backend на четыре временных ресурса;
7. проверить counts, связи document/chunk/storage, retrieval и citations;
8. удалить временные ресурсы только после отдельного подтверждения.

Никогда не восстанавливать поверх `clip_agent_2026`, `clip_agent_dense_v1`,
`clip_agent_sparse_v1` или текущего `STORAGE_ROOT`.
