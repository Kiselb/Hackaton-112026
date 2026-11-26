# AI-Agent Clip — Corporate Intelligence Lab

Локальный MVP гибридного поиска по корпоративным документам. Backend и frontend
запускаются в Windows, Ollama работает непосредственно в Windows, PostgreSQL, Qdrant и
OpenSearch могут работать в Docker Desktop/WSL.

> Прямой backend остаётся неаутентифицированным и должен слушать только `127.0.0.1`.
> Для доступа жюри используйте защищённый Caddy + Tailscale Funnel контур из
> [docs/CLIP010_TAILSCALE_DEMO.md](docs/CLIP010_TAILSCALE_DEMO.md) и только разрешённые данные.

## Реализованный контур

- загрузка UTF-8 TXT/MD, текстовых PDF и DOCX;
- parent/child chunking с точными offsets подсветки;
- BGE-M3 embeddings на CPU и dense search в Qdrant;
- BM25 в OpenSearch и reciprocal-rank fusion;
- финальная проверка `active=true` и `status=ready` по PostgreSQL;
- опциональный локальный CrossEncoder reranking после проверки активности;
- bounded AI-Агент по умолчанию с input guardrails, evidence assessment и fail-closed answer validation;
- request-level Multi-Query для AI-Агента: безопасное query expansion, межзапросный RRF и прозрачные затраты;
- request-level выбор локального Ollama или OpenAI Responses API с единым LLM-интерфейсом;
- stateful-уточнения с PostgreSQL-историей, повторным retrieval и изоляцией диалогов;
- подтверждаемая пользователем эскалация в службу качества, безопасный SMTP и
  публикация утверждённых экспертных разъяснений через тот же RAG pipeline;
- numbered sources, semantic grounding и проверка номеров цитат для обоих провайдеров;
- traces, timings и token counts в PostgreSQL;
- деактивация и восстановление документов;
- React UI поиска, кликабельных источников, feedback и административной аналитики;
- пагинируемый журнал запросов и viewer фактических LangGraph-траекторий;
- воспроизводимый smoke-test upload → ask → deactivate → ask → restore → ask.
- опциональный публичный demo ingress: production React через Caddy Basic Auth и Tailscale Funnel;
- неблокирующие конфигурируемые лимиты параллельных ask/ingest в публичном режиме.

AI-Агент и CrossEncoder reranker реализованы за независимыми флагами. После CLIP-012
AI-Агент включён по умолчанию для новых конфигураций; Базовый RAG сохранён для явного
сравнения. Существующий `.env` с `AGENT_ENABLED=false` не изменяется автоматически.

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

AI-Агент настраивается через `AGENT_ENABLED`, `AGENT_MAX_SEARCH_CALLS`,
`AGENT_MAX_RETRIES` и `AGENT_MAX_LLM_CALLS`. Defaults: `true`, 3, 1 и 4.
При исключении agent runtime возвращается контролируемый `technical_error`; автоматический
переход в Базовый RAG запрещён. Strict outcome и порядок безопасного обновления стенда:
[docs/CLIP012_STRICT_AGENT.md](docs/CLIP012_STRICT_AGENT.md).

Multi-Query разрешён сервером через `MULTI_QUERY_ENABLED=true`, но выключен для запроса
по умолчанию (`MULTI_QUERY_DEFAULT_ENABLED=false`). `MULTI_QUERY_MAX_VARIANTS=2`
включает исходный вопрос. Переключатель доступен в поиске только для AI-Агента; лимиты
agent search/LLM остаются общими и обязательная grounding validation не жертвуется ради
расширения: [docs/CLIP016_MULTI_QUERY.md](docs/CLIP016_MULTI_QUERY.md).

LLM по умолчанию остаётся `ollama`/`qwen3.5:9b`. Опциональный OpenAI включается только
backend-настройками, использует Responses API с `store=false` и серверный allowlist
моделей. Выбор provider/model действует на один запрос и не меняет глобальную
конфигурацию: [docs/CLIP013_LLM_PROVIDERS.md](docs/CLIP013_LLM_PROVIDERS.md).

Уточняющий диалог хранится в additive-таблицах `conversations` и
`conversation_turns`. По умолчанию незавершённый контекст действует 60 минут, допускает
не более двух уточняющих вопросов и трёх пользовательских ответов. Токен владения
передаётся только в `X-Conversation-Token`, а в PostgreSQL хранится его SHA-256-хеш:
[docs/CLIP014_CLARIFICATION_DIALOGUE.md](docs/CLIP014_CLARIFICATION_DIALOGUE.md).

Служба качества создаёт обращение только после явного подтверждения и только для
защищённого trace с `quality_review_required`/`conflict_detected`. SMTP выключен по
умолчанию, а экспертные действия требуют отдельного demo-кода. Утверждённый текст
становится обычным `Document` только после полной индексации:
[docs/CLIP015_QUALITY_SERVICE.md](docs/CLIP015_QUALITY_SERVICE.md).

Для локальной браузерной демонстрации можно отдельно установить
`AGENT_DEMO_OVERRIDE_ENABLED=true`. Тогда UI показывает request-level выбор режима;
глобальный `AGENT_ENABLED=false` при этом сохраняется. По умолчанию override выключен,
не меняет серверную настройку и допустим на loopback либо за защищённым Caddy ingress.

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

## Публичный стенд для жюри

CLIP-010 не создаёт вторую конфигурацию или корпус. Существующий backend остаётся на
loopback; Caddy раздаёт `frontend/dist`, защищает все UI/API/health маршруты одним
bcrypt Basic Auth и слушает отдельный loopback-порт; Tailscale Funnel публикует только
Caddy. Tailscale Serve не является публичной заменой Funnel.

```powershell
# После заполнения demo-параметров в существующем .env и verified backup:
.\scripts\demo\Start-PublicDemo.ps1 -RebuildFrontend -StartFunnel
.\scripts\demo\Get-PublicDemoStatus.ps1
$credential = Get-Credential -UserName jury
.\scripts\demo\Test-PublicDemo.ps1 -BaseUri https://<actual-node>.ts.net -Credential $credential
# Только для компонентов, запущенных новым launcher-ом:
.\scripts\demo\Stop-PublicDemo.ps1
```

Полная настройка, безопасный переход с ручного Funnel, повторный запуск после reboot,
диагностика и остановка: [docs/CLIP010_TAILSCALE_DEMO.md](docs/CLIP010_TAILSCALE_DEMO.md).
Настройка защищённых Swagger UI, ReDoc и OpenAPI JSON, включая ограничения
`Try it out` и процедуру Caddy reload/rollback:
[docs/CLIP011_API_DOCUMENTATION.md](docs/CLIP011_API_DOCUMENTATION.md).
Ngrok-вариант сохранён только как исторический контекст CLIP-008.

Verified backup основных PostgreSQL/Qdrant/OpenSearch/storage ресурсов выполняется
fail-closed процедурой CLIP-009. Сначала обязателен read-only preflight; без настроенного
OpenSearch snapshot repository backup не начинается:

```powershell
.\scripts\backup\Backup-Clip.ps1 -Preflight
.\scripts\backup\Backup-Clip.ps1 -Backup
```

Runbook, ограничения согласованности и план изолированного восстановления:
[docs/CLIP009_BACKUP.md](docs/CLIP009_BACKUP.md).

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
- `GET /readyz` — доступны три хранилища и default LLM-провайдер;
- `POST /api/v1/documents` — загрузка и синхронная индексация;
- `GET /api/v1/documents` — реестр;
- `POST /api/v1/documents/{id}/deactivate`;
- `POST /api/v1/documents/{id}/restore`;
- `POST /api/v1/ask`;
- `GET /api/v1/conversations/{id}` — история только с корректным ownership token;
- `POST /api/v1/conversations/{id}/close` — завершение без LLM-вызова;
- `GET /api/v1/capabilities` — demo override, безопасный список LLM и конфигурация Multi-Query;
- `GET /api/v1/admin/analytics` — агрегаты, mode share, latency/tokens и динамика;
- `GET /api/v1/admin/traces` — фильтруемый список с `limit`/`offset`;
- `GET /api/v1/admin/traces/{id}` — ответ, evidence и безопасная проекция trajectory;
- `POST /api/v1/traces/{id}/feedback` — оценка ответа владельцем trace.
- `POST /api/v1/quality-requests` — явное обращение владельца диалога;
- `GET/POST /api/v1/quality-requests...` — обработка, approve и publish с отдельным
  demo credential службы качества.

`status=ok` означает только техническое завершение. `/api/v1/ask` дополнительно возвращает
`outcome` и безопасный `reason_code`. Dashboard не выдаёт технический статус за
правильность ответа: без gold/expert signal поле корректности остаётся `not_measured`,
а обычный сформированный ответ получает quality status `unknown`. Схема классификации и
API описаны в [docs/reports/CLIP007_ANALYTICS.md](docs/reports/CLIP007_ANALYTICS.md).

OpenAPI локально: `http://127.0.0.1:8000/docs`. На публичном стенде использовать
только защищённый Caddy route `https://kronwerk.tail9a1e57.ts.net/docs`.

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
- токен диалога изолирует демонстрационные вкладки, но не заменяет корпоративную
  аутентификацию и ACL;
- прямой FastAPI не имеет аутентификации; публичный demo ingress имеет общий Basic Auth,
  но не ACL или production security;
- административные traces доступны глобально в локальном MVP; фильтры не являются ACL;
- нет очереди ingestion и автоматической reconciliation между тремя хранилищами;
- SMTP и публикация экспертного источника синхронны; demo approval token не заменяет
  корпоративную identity/SSO/ACL;
- evidence assessment и проверка номера цитаты не заменяют экспертную entailment-проверку;
- baseline измерен на синтетических документах и не является оценкой качества на
  производственном корпусе;
- production bundle раздаётся Caddy только в явно включённом публичном demo-контуре.

Синтетические документы находятся в `examples/`. Они не являются данными организатора.
