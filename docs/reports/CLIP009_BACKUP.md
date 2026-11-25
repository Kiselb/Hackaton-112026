# CLIP-009 — отчёт по verified backup

Дата: 2026-11-24; обновлено 2026-11-25. Исходный Git SHA: `123cea7`.

## 1. Что реализовано в коде

- добавлен fail-closed PowerShell 5.1 script с отдельными `-Preflight` и `-Backup`;
- ограничены точные PostgreSQL/Qdrant/OpenSearch resources и loopback endpoints;
- предусмотрены проверка quiesce, PostgreSQL 17 custom dump/list, Qdrant snapshot,
  побайтово проверяемая копия storage, OpenSearch index-only snapshot и копия физического
  repository;
- manifest содержит версии, Git, статусы, размеры и SHA-256, но не secrets/DSN/абсолютный
  storage path;
- каждый запуск создаёт отдельный приватный каталог вне Git; старые копии не удаляются;
- добавлены статические тесты destructive-operation guards и документация восстановления;
- исправлены Windows PowerShell 5.1 parsing Docker mounts, Docker Desktop host-path
  conversion и регистронезависимый конфликт repository variables;
- pytest fail closed отклоняет `--basetemp` внутри рабочего `STORAGE_ROOT`.

RAG, LangGraph, модели, API, frontend, Docker Compose и `.env` не изменялись.

## 2. Какие резервные копии реально созданы

**Ни одной.** Финальный Preflight прошёл, но `-Backup` не был разрешён и не запускался.
Snapshot/dump и каталог `clip009-*` не создавались; подготовленность процедуры не
выдаётся за фактическую резервную копию.

## 3. Какие проверки реально выполнены

До реализации установлено:

| Проверка | Фактический результат |
|---|---|
| Git status | clean, `main`, `123cea7` |
| PostgreSQL target | `clip_agent_2026`, PostgreSQL 17.11 |
| PostgreSQL size / active indexing | 11,040,435 bytes / `0` indexing rows |
| PostgreSQL clients в PATH | отсутствуют; совместимые 17 clients есть в container |
| Qdrant collection | `green`, 8 points, vector size 1024/Cosine |
| OpenSearch index | `green`, open, 8 documents, OpenSearch 3.8.0 |
| `STORAGE_ROOT` | существует; 18 files, 10,516 bytes |
| FastAPI/Caddy ports 8000/8080 | listeners отсутствуют |
| OpenSearch repositories | отсутствуют (`{}`) |
| OpenSearch `path.repo` | не настроен |
| PowerShell parser | passed |
| CLIP-009 preflight | ожидаемо failed closed на repository |

После реализации выполнено:

| Проверка | Результат |
|---|---|
| CLIP-009 профильные тесты | 6 passed |
| полный pytest | 57 passed, 2 существующих FastAPI `on_event` warnings |
| Ruff `backend tests scripts` | passed |
| Python dependencies | `No broken requirements found` |
| frontend TypeScript + Vite build | passed; 29 modules, JS 206.78 kB (64.91 kB gzip) |
| PowerShell parser | passed |
| Финальный CLIP-009 `-Preflight` | passed, exit code 0 |
| `git diff --check` | passed |
| `npm audit --omit=dev` | not run: npm отсутствует; bundled pnpm не принимает `package-lock.json` |

Проектная `.venv` ссылается на отсутствующий base Python 3.12. Поэтому pytest/pip check
выполнены bundled Python 3.12.14 с существующими packages `.venv` и явно подключённым
`backend`; для pywin32 добавлены только process-local DLL paths. Frontend собран bundled
Node напрямую. Это отклонение среды, а не изменение проекта.

## 4. Какие компоненты требуют ручных действий

OpenSearch prerequisite выполнен и repository проверен. Следующий этап — только отдельное
явное разрешение владельца на `-Backup` при сохранённых остановленными путях записи.
Скрипт никогда не останавливает процессы сам.

### CLIP-009-INFRA: состояние перед применением

Read-only диагностика 2026-11-24:

| Поле | Фактическое значение |
|---|---|
| Compose | `D:\EDUCATION\AIU Стажировка Водоканал\rvk-office-ai\RAG\docker-compose.yml` |
| Project / service | `rag` / `opensearch` |
| Container | `rag-opensearch-1`, ID `a6c7fd4dcdd5...` |
| Data volume | `rag_opensearch_data` → `/usr/share/opensearch/data`, read/write |
| OpenSearch | 3.8.0, cluster `green`, 15/15 active shards, 0 unassigned |
| Индексы | 14, все `open/green`; `clip_agent_sparse_v1` — 8 docs |
| Snapshot/recovery | 0 / 0 active |
| `path.repo` / repositories | не настроен / `{}` |
| Snapshot host path | `C:\Users\mvkiselev\Documents\AI-Agent-Clip-Backups\opensearch-repository` |
| Свободное место | около 1.37 TiB |
| Compose validation | current и in-memory candidate: exit 0 |
| Порты записи AI-Agent Clip | 8000 и 8080 сейчас имеют listeners |

Контрольные индексы до изменения:

| Индекс | UUID | Docs |
|---|---|---:|
| `.plugins-ml-config` | `swyXVOzFRWqXaRTqLfscaQ` | 1 |
| `clip_agent_eval_corporate_v1_sparse` | `8geyWoUmSm6NXx_4AeNWRg` | 4 |
| `clip_agent_eval_corporate_v2_sparse` | `n_7MQGmPSWWxI7l0vtZb5Q` | 26 |
| `clip_agent_sparse_v1` | `cNQIiMjcS1KFGUZR712JpQ` | 8 |
| `clip_agent_sparse_v1_clip002` | `Xzh2ABWTR0K9Yyb5hHmD-Q` | 4 |
| `top_queries-2026.11.16-89287` | `ddb1vQvITeygsVw0ZgXcPQ` | 13 |
| `top_queries-2026.11.17-89288` | `bAePY94IQnaUNivlQeaVLA` | 9 |
| `top_queries-2026.11.18-89289` | `KGcvMJ1MQwO169fx5b4UhA` | 13 |
| `top_queries-2026.11.19-89290` | `qCWxl5t7QbiwM8BZZb9T6Q` | 13 |
| `top_queries-2026.11.20-89312` | `z5WKp3plT-quEzsiKrNaZw` | 13 |
| `top_queries-2026.11.21-89313` | `0Y5IkxTlSx-3lG6QYYMhzQ` | 13 |
| `top_queries-2026.11.22-89314` | `b1OqZYx0QCSrb2Nn0HcPMA` | 13 |
| `top_queries-2026.11.23-89315` | `42uWMFMWSpeCg1JiHuB9pQ` | 322 |
| `top_queries-2026.11.24-89316` | `wFqzw25UQx-dbd3-FZrDOg` | 13 |

Внешний Compose и Documents не изменены: sandbox не разрешает запись, а обходить его
запрещено заданием. Владелец должен создать защищённую копию Compose вне Git, создать
snapshot-каталог и применить точную дельту из `docs/CLIP009_BACKUP.md`. После этого
требуется отдельное подтверждение перед tag/recreate общего OpenSearch. До подтверждения
не зарегистрированы `clip_backup` и не запускался CLIP-009 `-Preflight` повторно.

### CLIP-009-INFRA: применение и диагностическая остановка

Владелец подтвердил ручное применение/backup Compose, создание каталога, quiesce и явно
разрешил recreate только `rag/opensearch`. Фактически выполнено:

```text
docker image tag sha256:bcc1797519726ceb6d651d4a3e60b7c30da91793914a8dfe75fd441d4f641509 opensearchproject/opensearch:3.8.0
docker compose -p rag -f <verified-compose> up -d --no-deps --force-recreate --pull never opensearch
```

Команды завершились с exit 0. Старый/new container ID:
`a6c7fd4dcdd5...` → `68a440de48fd...`; image ID остался
`sha256:bcc1797519726ceb6d651d4a3e60b7c30da91793914a8dfe75fd441d4f641509`.
Data volume остался `rag_opensearch_data`; добавлен только bind на
`/mnt/clip-backups`. PostgreSQL, PostgreSQL test и Qdrant сохранили прежние IDs и status
`running`.

Post-check: OpenSearch 3.8.0, cluster `green`, 15 active/0 unassigned shards,
`path.repo=/mnt/clip-backups`, каталог writable. Имена, UUID, `open/green` и live
`docs.count` всех 14 видимых индексов совпали с baseline. Обнаружены физические
расхождения Lucene:

- `top_queries-2026.11.23-89315`: `docs.deleted` 6 → 4 при неизменных UUID и
  `docs.count=322`; `_count=322`, segment stats: 322 live/4 deleted, ещё 2 tombstones в
  committed `search=false` segment;
- `top_queries-2026.11.17-89288`: `store.size` 82.6 kB → 23 kB при неизменных UUID,
  `docs.count=9`, `docs.deleted=0`.

Active merges/tasks/recovery/snapshots и pending cluster tasks отсутствуют. Startup logs
показывают recovery существующих shards и переход RED → GREEN; corruption/data-loss
ошибок нет. Из-за stop-rule следующие мутации не выполнялись: `clip_backup` не
зарегистрирован, `_verify` и CLIP-009 `-Preflight` не запущены, `-Backup`/restore не
выполнялись. Требуется решение владельца, считать ли физическую segment cleanup
допустимой и продолжить регистрацию repository.

Владелец принял физические изменения Lucene и разрешил регистрацию/preflight.
Фактические результаты:

| Проверка | Результат |
|---|---|
| `PUT /_snapshot/clip_backup` | `acknowledged=true` |
| `GET /_snapshot/clip_backup` | `type=fs`, `location=/mnt/clip-backups` |
| `POST /_snapshot/clip_backup/_verify` | passed, node `68a440de48fd` |
| Mount внутри OpenSearch | directory существует и writable |
| CLIP-009 `-Preflight` | финальный запуск passed, exit code 0 |
| CLIP-009 `-Backup` | не запускался |
| Restore | не выполнялся |
| Созданные `clip009-*` каталоги | 0 |

Промежуточные причины fail-closed Preflight — три подтверждённых дефекта Windows
PowerShell/Docker Desktop в
`scripts/backup/Backup-Clip.ps1`:

1. `Get-OpenSearchRepositoryInfo([string]$Repository, ...)` присваивает object в
   `$repository`; имена переменных PowerShell регистронезависимы, типизированный параметр
   сохраняет string type. Строка 198 не может прочитать `.type`.
2. После исправления имени следующий blocker — Docker inspect source
   `/run/desktop/mnt/host/c/Users/...`; текущий `ConvertTo-CanonicalPath` получает
   `D:\run\desktop\...` и не видит существующий Windows-каталог на `C:`;
3. Windows PowerShell 5.1 возвращает Docker mounts как вложенный array при прямом
   `@(... | ConvertFrom-Json)`.

Все три дефекта исправлены с сохранением fail-closed проверок: mounts разворачиваются
по одному, repository bind требует единственного точного `Destination`, `Type=bind`,
поддерживаемого source path и существующего host-каталога. Три подтверждённых pytest
artifacts с нулевыми PostgreSQL references удалены по отдельным разрешениям. Полная
инвентаризация `STORAGE_ROOT`: 17 каталогов, 17 файлов, 37,315 bytes, 0 traversal errors,
0 reparse points, 0 pytest/duplicate artifacts. Финальный Preflight прошёл; backup данных
не начинался.

## 5. Можно ли считать backup полным

**Нет.** Ни один обязательный component backup ещё не создан. OpenSearch prerequisite и
Preflight выполнены, но ненулевой размер, checksum и archive list будущего запуска также
не заменят изолированный restore drill.

## 6. Можно ли безопасно переходить к публичной активации ngrok

**Нет.** До публичной активации требуется `manifest.complete=true` для фактического
запуска и отдельные проверки CLIP-008 (Caddy/ngrok/auth/external QA). Текущий результат
подготавливает процедуру, но не является готовым verified backup.
