# CLIP-008 — Public Demo Stand report

Дата: 2026-11-24

Базовый commit: `4ba2821e0ffbb635e0bfeda8d19d4a565093f322`

Изменения retrieval/prompt/model/embeddings: отсутствуют

Метрики качества RAG: `not measured` (повторная evaluation не требовалась)

## Результат

Реализована конфигурация одного публичного ingress поверх существующего AI-Agent Clip:

- Caddy слушает `127.0.0.1:<DEMO_PROXY_PORT>`, раздаёт production React, делает SPA
  fallback и проксирует только в loopback FastAPI;
- site-wide bcrypt Basic Auth выполняется до UI, API, health/readiness и статических
  файлов;
- username фиксирован как `jury`, bcrypt-хеш берётся из существующего `.env`, plaintext
  не принимается launcher-ом;
- `Authorization` удаляется из Caddy access log и до отправки запроса в FastAPI;
- ngrok launcher публикует только Caddy и требует фактический стабильный домен владельца;
- внешний URL не зашит во frontend; все API-вызовы остаются same-origin;
- публичный режим использует прежние DSN, Qdrant collection, OpenSearch index и storage;
- ask и ingestion получили неблокирующие process-local limits с HTTP 429 и гарантированным
  освобождением слота;
- upload ограничен согласованно: Caddy учитывает multipart overhead, FastAPI проверяет
  точный размер и возвращает 413;
- обычный локальный запуск сохраняет неограниченный gate и прежний default upload 25 MiB;
  launcher публичного режима устанавливает документированный default 20 MiB;
- подготовлены fail-closed Windows launcher и безопасная proxy-check процедура без
  успешных POST;
- документированы backup, смена пароля, запуск, остановка, Windows restart и trust boundary.

Алгоритмы RAG/Agent, форматы успешных API-ответов и схема БД не изменены. Новые ответы:
429 при занятом публичном gate и 413 при превышении upload limit.

## Изменённые файлы

- `.env.example` — публичные флаги, hash, порты и limits без секретов;
- `backend/clip_agent/config.py` — typed settings;
- `backend/clip_agent/load_control.py` — неблокирующий gate;
- `backend/clip_agent/main.py` — ask/ingest dependencies и configurable upload limit;
- `scripts/demo/Caddyfile` — auth, static SPA, proxy, logging, body/time limits;
- `scripts/demo/Start-PublicDemo.ps1` — preflight/build/start/ngrok/fail-closed checks;
- `scripts/demo/Test-PublicDemo.ps1` — auth/routing checks через Caddy;
- `tests/test_load_control.py`, `tests/test_demo_configuration.py` — unit/static safeguards;
- `frontend/src/App.tsx` — корректное описание security boundary в footer;
- `docs/CLIP008_PUBLIC_DEMO.md` — runbook;
- `README.md`, `ARCHITECTURE.md`, `DEMO.md` — фактическая новая схема;
- этот отчёт.

## Фактически выполненные проверки

| Проверка | Результат |
|---|---|
| `pytest` | 51 passed, 2 существующих FastAPI `on_event` deprecation warnings |
| `ruff check backend tests scripts` | passed |
| `pip check` | `No broken requirements found` |
| frontend TypeScript + Vite production build | passed; 29 modules, JS 206.78 kB (64.91 kB gzip) |
| `npm audit --omit=dev` | 0 vulnerabilities |
| PowerShell parser, оба demo scripts | passed |
| launcher с текущим `PUBLIC_DEMO_ENABLED=false` | ожидаемо fail-closed до запуска процессов |
| `git diff --check` | passed |

Проектная `.venv` содержит пакеты, но её launcher ссылается на отсутствующий
`C:\Users\...\Python312\python.exe`. Поэтому pytest и pip check выполнены bundled Python
3.12.14 с подключёнными site-packages этой `.venv`; это отклонение окружения, а не
ошибка тестов. Перед активацией владелец должен восстановить Python 3.12 и `.venv`.

## Security checks

Автоматические unit/static проверки подтверждают:

- loopback listener Caddy и loopback upstream FastAPI;
- bcrypt hash placeholder, пустой hash в `.env.example`;
- auth расположен в ordered `route` раньше body/proxy/file handlers;
- удаление `Authorization` upstream и из access log;
- наличие негативных проверок для ask, documents, deactivate/restore, traces, feedback,
  health, неверного пароля и позитивных GET;
- gate немедленно возвращает 429, не создаёт очередь и снова доступен после выхода;
- отключённый public mode не ограничивает обычный локальный режим;
- oversized upload отклоняется до записи в storage.

Реальный `Test-PublicDemo.ps1` через Caddy **не выполнен**: Caddy и ngrok отсутствуют на
машине. Следовательно, фактические HTTP 401/200, `caddy validate`, внешний HTTPS и
редакция реального access log пока `not tested`.

## Browser QA

Выполнен реальный browser QA собранного production bundle через loopback Vite preview:

- desktop `1440×900`: UI загрузился, четыре раздела видимы, horizontal overflow нет,
  console warnings/errors нет;
- mobile `390×844`: все четыре navigation buttons видимы в пределах viewport, textarea
  помещается, horizontal overflow нет;
- после повторной сборки новый footer подтверждён в DOM.

Это проверка production React bundle, но не Caddy-auth и не полного API lifecycle. Backend
не подключался к preview, поэтому Dashboard/Traces/Documents/Feedback, Baseline/Agent и
SPA reload через Caddy в браузере не проверены.

## Данные и ресурсы

Основные PostgreSQL/Qdrant/OpenSearch/storage не читались и не изменялись тестовым
lifecycle. Smoke/evaluation не запускались. Тесты использовали unit/in-memory данные и
ignored `storage/tmp` только для pytest temp.

Конфигурация по-прежнему указывает единственные рабочие ресурсы:

| Компонент | Ресурс |
|---|---|
| PostgreSQL | `clip_agent_2026` |
| Qdrant | `clip_agent_dense_v1` |
| OpenSearch | `clip_agent_sparse_v1` |
| Files | текущий `STORAGE_ROOT` |
| Ollama | `http://127.0.0.1:11434` |

Backup основных ресурсов фактически не создавался: это осознанно оставлено владельцу до
публикации, потому что backup содержит реальные данные и требует выбора защищённого
каталога/OpenSearch snapshot repository. Runbook содержит non-destructive команды.

## Компоненты и ручные действия владельца

На проверенной машине отсутствуют `caddy`, `ngrok`, системные `node`/`npm` и рабочий base
Python проектной `.venv`. Установки и системные настройки не выполнялись.

Перед публичным включением владелец должен:

1. восстановить Python 3.12 и `.venv`; установить Caddy 2.10+, ngrok и Node.js/npm;
2. создать PostgreSQL/storage/Qdrant/OpenSearch backups и проверить их результат;
3. выполнить интерактивный `caddy hash-password`, добавить hash и demo settings в
   существующий `.env`, не меняя DSN/index/collection/storage;
4. связать ngrok с аккаунтом и указать его фактический стабильный domain;
5. запустить локальный stand, выполнить `Test-PublicDemo.ps1` и browser QA через Caddy;
6. остановить локальную проверку, запустить с `-StartTunnel`, повторить auth-check по
   HTTPS с внешней сети;
7. на изолированных временных ресурсах выполнить полный document lifecycle/Agent smoke;
8. только после этого передать URL и пароль жюри по отдельным каналам.

## Готовность и ограничения

Конфигурация Caddy и ngrok launcher подготовлены, но не провалидированы установленными
бинарниками. Публичный URL не получен. Итоговый статус:

**Код и runbook защищённого стенда готовы; локальная и внешняя активация ожидают установки
компонентов, восстановления Python 3.12 `.venv`, backup и действий владельца в ngrok.**

Оставшиеся риски: один общий пароль, нет ACL/malware scan/rate limiter по IP, ngrok входит
в trust boundary, одна рабочая станция/GPU, синхронная нетранзакционная индексация и
реальные общие изменения корпуса без автоматического rollback.
