# AI-Agent Clip — Corporate Intelligence Lab

Локальный MVP гибридного поиска по корпоративным документам. Backend и frontend
запускаются в Windows, Ollama работает непосредственно в Windows, PostgreSQL, Qdrant и
OpenSearch могут работать в Docker Desktop/WSL.

> Демонстрационный MVP без аутентификации. Запускайте только на `127.0.0.1` и
> используйте синтетические или разрешённые документы.

## Реализованный контур

- загрузка UTF-8 TXT/MD, текстовых PDF и DOCX;
- parent/child chunking с точными offsets подсветки;
- BGE-M3 embeddings на CPU и dense search в Qdrant;
- BM25 в OpenSearch и reciprocal-rank fusion;
- финальная проверка `active=true` и `status=ready` по PostgreSQL;
- локальный ответ Ollama с numbered sources и проверкой номеров цитат;
- traces, timings и token counts в PostgreSQL;
- деактивация и восстановление документов;
- React UI поиска, источников и административного реестра;
- воспроизводимый smoke-test upload → ask → deactivate → ask → restore → ask.

Архитектурные планы LangGraph, multi-query judge и reranker из
[ARCHITECTURE.md](ARCHITECTURE.md) пока не реализованы: сначала зафиксирован
воспроизводимый baseline существующего RAG-контура на целевой Qwen-модели.

## Изоляция данных

Приложение использует только отдельные ресурсы:

| Хранилище | Имя |
|---|---|
| PostgreSQL | `clip_agent_2026` |
| Qdrant | `clip_agent_dense_v1` |
| OpenSearch | `clip_agent_sparse_v1` |

База `rag` и индексы предыдущих проектов не используются. Настройки берутся из
`.env`; файл исключён из Git.

## Быстрый запуск в Windows

Требуются Python строго 3.12, Node.js 20+, запущенные Docker-сервисы и Ollama:

```powershell
Copy-Item .env.example .env
# Указать локальный пароль PostgreSQL в .env.

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

ollama pull qwen3.5:9b

Set-Location frontend
npm install
Set-Location ..
```

Backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn clip_agent.main:app --host 127.0.0.1 --port 8000
```

Frontend в другом терминале:

```powershell
Set-Location frontend
npm run dev
```

Открыть `http://127.0.0.1:5173`. Подробные команды создания отдельной базы,
диагностики портов, Ollama и GPU находятся в
[docs/LOCAL_SETUP_WINDOWS.md](docs/LOCAL_SETUP_WINDOWS.md).

## Проверки

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check backend tests scripts

Set-Location frontend
npm run build
npm audit --omit=dev
Set-Location ..

.\.venv\Scripts\python.exe scripts\smoke_test.py --model qwen3.5:9b --output smoke-results-qwen.json
```

Smoke следует запускать на свежих проектных ресурсах: повторный запуск загружает новые
документы. Он проверяет retrieval, LLM, citations, traces, деактивацию и восстановление,
а также сохраняет реальные timings и resource snapshots.

## API

- `GET /healthz` — процесс запущен;
- `GET /readyz` — доступны три хранилища, Ollama и настроенная модель;
- `POST /api/v1/documents` — загрузка и синхронная индексация;
- `GET /api/v1/documents` — реестр;
- `POST /api/v1/documents/{id}/deactivate`;
- `POST /api/v1/documents/{id}/restore`;
- `POST /api/v1/ask`;
- `GET /api/v1/admin/analytics`;
- `GET /api/v1/admin/traces`.

OpenAPI: `http://127.0.0.1:8000/docs`.

## Текущее состояние CLIP-002

На проверенной машине backend, frontend, отдельная БД/индексы, dense+sparse retrieval,
document lifecycle и traces работают. `qwen3.5:9b` установлена из официального Ollama
registry. Для `/api/chat` приложение отправляет конфигурируемые `think: false`,
`temperature: 0` и `num_predict: 512`; пустые ответы и причина завершения сохраняются
в trace. Изолированный smoke-test подтвердил обе ставки пени, правильные источники,
отсутствие выдуманного приоритета, деактивацию/восстановление и GPU inference.

Фактические результаты и ограничения:
[docs/BASELINE_RESULTS.md](docs/BASELINE_RESULTS.md).

## Ограничения

- нет OCR, распознавания таблиц и сканов;
- нет аутентификации, ACL и production security;
- нет очереди ingestion и автоматической reconciliation между тремя хранилищами;
- синтаксическая проверка номера цитаты не доказывает поддержку утверждения источником;
- baseline измерен на синтетических документах и не является оценкой качества на
  производственном корпусе;
- frontend доступен только как dev server или собранный `frontend/dist`; раздача
  production bundle backend-ом не настроена.

Синтетические документы находятся в `examples/`. Они не являются данными организатора.
