# CLIP-009 — отчёт по verified backup

Дата: 2026-11-24. Исходный Git SHA: `123cea7`.

## 1. Что реализовано в коде

- добавлен fail-closed PowerShell 5.1 script с отдельными `-Preflight` и `-Backup`;
- ограничены точные PostgreSQL/Qdrant/OpenSearch resources и loopback endpoints;
- предусмотрены проверка quiesce, PostgreSQL 17 custom dump/list, Qdrant snapshot,
  побайтово проверяемая копия storage, OpenSearch index-only snapshot и копия физического
  repository;
- manifest содержит версии, Git, статусы, размеры и SHA-256, но не secrets/DSN/абсолютный
  storage path;
- каждый запуск создаёт отдельный приватный каталог вне Git; старые копии не удаляются;
- добавлены статические тесты destructive-operation guards и документация восстановления.

RAG, LangGraph, модели, API, frontend, Docker Compose и `.env` не изменялись.

## 2. Какие резервные копии реально созданы

**Ни одной.** Preflight остановился до создания каталога и до любых snapshot/dump:
OpenSearch repository `clip_backup` отсутствует. Частичный backup не выдавался за
успешный.

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
| CLIP-009 static tests | 3 passed |
| полный pytest | 54 passed, 2 существующих FastAPI `on_event` warnings |
| Ruff `backend tests scripts` | passed |
| Python dependencies | `No broken requirements found` |
| frontend TypeScript + Vite build | passed; 29 modules, JS 206.78 kB (64.91 kB gzip) |
| PowerShell parser | passed |
| `git diff --check` | passed |
| `npm audit --omit=dev` | not run: npm отсутствует; bundled pnpm не принимает `package-lock.json` |

Проектная `.venv` ссылается на отсутствующий base Python 3.12. Поэтому pytest/pip check
выполнены bundled Python 3.12.14 с существующими packages `.venv` и явно подключённым
`backend`; для pywin32 добавлены только process-local DLL paths. Frontend собран bundled
Node напрямую. Это отклонение среды, а не изменение проекта.

## 4. Какие компоненты требуют ручных действий

Только prerequisite OpenSearch требует изменения инфраструктуры владельцем: добавить в
фактический внешний Compose-файл отдельный host bind вне Git/Docker volumes и
`path.repo`, затем пересоздать только OpenSearch и зарегистрировать/verify repository.
Точный безопасный план приведён в `docs/CLIP009_BACKUP.md`. Это не выполнено, поскольку
операция требует перезапуска общего контейнера и явного согласия.

После этого владелец должен повторить `-Preflight`, сохранить пути записи остановленными
и явно запустить `-Backup`. Скрипт никогда не останавливает процессы сам.

## 5. Можно ли считать backup полным

**Нет.** Ни один обязательный component backup ещё не создан; OpenSearch prerequisite
не выполнен. Ненулевой размер, checksum и archive list в будущем также не заменят
изолированный restore drill.

## 6. Можно ли безопасно переходить к публичной активации ngrok

**Нет.** До публичной активации требуется `manifest.complete=true` для фактического
запуска и отдельные проверки CLIP-008 (Caddy/ngrok/auth/external QA). Текущий результат
подготавливает процедуру, но не является готовым verified backup.
