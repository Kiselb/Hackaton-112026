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
- опциональный локальный CrossEncoder reranking после проверки активности;
- опциональный bounded LangGraph agent с evidence assessment и answer validation;
- локальный ответ Ollama с numbered sources и проверкой номеров цитат;
- traces, timings и token counts в PostgreSQL;
- деактивация и восстановление документов;
- React UI поиска, источников и административного реестра;
- воспроизводимый smoke-test upload → ask → deactivate → ask → restore → ask.

Bounded LangGraph agent и CrossEncoder reranker реализованы за независимыми флагами
и оставлены выключенными по умолчанию. CLIP-006 улучшил корректность отказов, но
добавил около 3,5 секунды mean latency и показал небольшую регрессию автоматической
required-facts метрики, поэтому baseline остаётся default.

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

Reranker настраивается через `RERANK_ENABLED`, `RERANK_MODEL`,
`RERANK_MODEL_REVISION`, `RERANK_DEVICE`, `RERANK_CANDIDATES` и
`RERANK_TOP_K`; без явного включения модель не загружается.

Agent настраивается через `AGENT_ENABLED`, `AGENT_MAX_SEARCH_CALLS`,
`AGENT_MAX_RETRIES` и `AGENT_MAX_LLM_CALLS`. Defaults: `false`, 3, 1 и 4.
При исключении agent runtime запрос контролируемо выполняется существующим RAG,
а причина fallback сохраняется в trace.

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

## Evaluation Lab (CLIP-003—005)

Воспроизводимый baseline на 18 вручную сверенных сценариях запускается без отдельного
backend-процесса:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval/datasets/corporate_v1.jsonl
```

Усложнённый `corporate_v2` хранится отдельно от `examples/`: 26 синтетических
документов, 36 вопросов, устойчивые document/fragment ID и graded relevance.

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval/datasets/corporate_v2.jsonl

$env:RERANK_ENABLED='true'
$env:RERANK_MODEL_REVISION='953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'
.\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval/datasets/corporate_v2.jsonl

$env:RERANK_ENABLED='false'
$env:AGENT_ENABLED='true'
.\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval/datasets/corporate_v2.jsonl
```

Runner использует отдельные PostgreSQL schema, Qdrant collection, OpenSearch index и
storage, проверяет SHA-256 корпуса и не меняет пользовательские индексы. Он сохраняет
run.json, cases.jsonl и report.md в исключённом из Git каталоге eval/results/.
Run v2 также сохраняет SHA-256 фактического source tree и dirty-status, поэтому
незакоммиченный эксперимент нельзя спутать с чистым HEAD.
Сравнение двух прогонов: scripts/evaluate.py --compare RUN_A RUN_B.

Методика: [docs/EVALUATION_LAB.md](docs/EVALUATION_LAB.md). Протокол:
[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md). Фактический baseline:
[docs/reports/CLIP003_BASELINE.md](docs/reports/CLIP003_BASELINE.md).
Эксперимент с reranking:
[docs/reports/CLIP005_RERANKING.md](docs/reports/CLIP005_RERANKING.md).
Эксперимент с bounded agent:
[docs/reports/CLIP006_AGENT.md](docs/reports/CLIP006_AGENT.md).

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
- evidence assessment и проверка номера цитаты не заменяют экспертную entailment-проверку;
- baseline измерен на синтетических документах и не является оценкой качества на
  производственном корпусе;
- frontend доступен только как dev server или собранный `frontend/dist`; раздача
  production bundle backend-ом не настроена.

Синтетические документы находятся в `examples/`. Они не являются данными организатора.
