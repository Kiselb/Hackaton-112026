# CLIP-013 — LLM-провайдеры Ollama и OpenAI

## Граница изменения

CLIP-013 добавляет request-level выбор языковой модели, не меняя retrieval, SQL visibility
gate, prompts корпоративных фактов и строгую публикацию ответа CLIP-012-FIX. По умолчанию
остаётся локальный `ollama`/`qwen3.5:9b`. OpenAI выключен, пока владелец явно не включит
его и не задаст ключ только на backend.

Публичный стенд, действующий `.env`, Caddy, Funnel, процессы и рабочие хранилища в этой
задаче не обновлялись.

## Архитектура

`backend/clip_agent/llm.py` содержит:

- allowlist-разрешение `LlmSelection(provider, model)`;
- адаптер Ollama `/api/chat`;
- адаптер официального Python SDK OpenAI и Responses API;
- request-scoped `LlmSession`, который фиксирует одну пару provider/model для всего
  запроса, суммирует вызовы, input/output tokens и latency;
- безопасную классификацию timeout, network, authentication, rate limit, 5xx, refusal,
  incomplete, output limit, malformed и empty response.

Baseline RAG и LangGraph используют один интерфейс. Агент получает тот же экземпляр
`LlmSession` на planning, evidence assessment, composition, citation repair и grounding
validation. Автоматического перехода OpenAI → Ollama или Ollama → OpenAI нет.

Структурированные planning и grounding-запросы передают JSON Schema. Для OpenAI это
Structured Outputs; результат всё равно повторно разбирается и проверяется сервером.
Инвариант CLIP-012-FIX сохранён: `valid=true` вместе с непустыми
`unsupported_claims`/`scope_errors` считается невалидным ответом.

## Конфигурация

Новые параметры `.env.example`:

```dotenv
OLLAMA_TIMEOUT_SECONDS=300
LLM_DEFAULT_PROVIDER=ollama
LLM_MAX_TOTAL_TOKENS_PER_REQUEST=12000
OPENAI_ENABLED=false
OPENAI_API_KEY=
OPENAI_DEFAULT_MODEL=gpt-6.2-sol
OPENAI_ALLOWED_MODELS=gpt-6.2-sol,gpt-6.2-astra
OPENAI_TIMEOUT_SECONDS=45
OPENAI_MAX_OUTPUT_TOKENS=2048
OPENAI_REASONING_EFFORT=medium
```

`OPENAI_API_KEY` читается `SecretStr`-полем только на backend. Не передавайте ключ в чат,
JSON запроса, браузер, trace или Git. Для текущего семейства разрешены reasoning effort
`low`, `medium`, `high`, `xhigh`, `max`; `none` не предлагается, поскольку Astra его не
принимает.

`OPENAI_ALLOWED_MODELS` — разделённый запятыми серверный allowlist. CLIP-013 содержит
официально подтверждённые model IDs:

- `gpt-6.2-sol` — default OpenAI model;
- `gpt-6.2-astra` — более мощная и дорогая альтернатива.

Обе модели и Responses API подтверждены [каталогом моделей OpenAI](https://developers.openai.com/api/docs/models).
Поддержка Structured Outputs и ограничений Astra описана на
[странице модели](https://developers.openai.com/api/docs/models/gpt-6.2-astra).
Фактический доступ конкретного API project определяется только live-вызовом; наличие
ключа само по себе его не доказывает.

## Responses API и безопасность

Интеграция использует `openai==2.54.0` и `client.responses.create()` со следующими
ограничениями:

- `store=False` для каждого корпоративного запроса;
- `max_output_tokens` и timeout из настроек;
- `max_retries=0`: платные неограниченные повторы отсутствуют;
- внешние tools, web search и file search не передаются;
- `temperature` не передаётся reasoning-моделям;
- incomplete, refusal и пустой `output_text` завершаются fail-closed;
- технический ответ клиенту содержит только безопасную категорию, без exception text,
  ключа, заголовков или endpoint.

Серверный `LLM_MAX_TOTAL_TOKENS_PER_REQUEST` ограничивает дальнейшие LLM-вызовы после
достижения суммарного фактического usage. Это operational token guard, а не гарантированный
денежный лимит: стоимость зависит от модели, тарифа и политики OpenAI. Дополнительно агент
сохраняет `AGENT_MAX_LLM_CALLS`.

Официальная схема параметров, `incomplete_details`, usage и JSON Schema находится в
[Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create).

## API и интерфейс

`POST /api/v1/ask` принимает необязательные поля:

```json
{
  "question": "Кто назначает руководителя проекта?",
  "mode": "agent",
  "llm_provider": "openai",
  "llm_model": "gpt-6.2-sol"
}
```

Если поля отсутствуют, применяется server default. Произвольные provider/model, URL и
endpoint не принимаются. OpenAI без `OPENAI_ENABLED=true` и backend-ключа возвращает
контролируемую ошибку доступности. Ответ дополнительно содержит фактические
`llm_provider`, `llm_model`; `metrics` содержит эти поля, usage и provider latency.

`GET /api/v1/capabilities` публикует только default, enabled/configured и model allowlist.
Ключи и внутренние URL отсутствуют. UI строит переключатель из capabilities; состояние
живёт в React-клиенте и отправляется только со следующим запросом, поэтому переключение
не меняет настройки других пользователей.

`/readyz` требует готовность только default LLM-провайдера. При default Ollama отсутствие
OpenAI-ключа отображается как `disabled`/`not_configured`, но не делает сервис неготовым.
Remote OpenAI readiness не вызывает платный запрос: `configured` означает только наличие
включённой backend-конфигурации, а runtime-доступ проверяется фактическим запросом.

## Traces и usage

Для нового запроса в `SearchTrace.details` сохраняются:

- `llm_provider`, `llm_model`, `llm_calls`;
- суммарные `input_tokens`, `output_tokens`, provider latency;
- безопасный `provider_error_category`;
- прежние outcome, reason code, trajectory, search/retry counters и validation.

Старые traces без этих полей продолжают сериализоваться. `prompt_tokens` и
`completion_tokens` остаются агрегатами всех LLM-вызовов запроса; число пользовательских
запросов не смешивается с `llm_calls`.

## Проверка

Основная регрессия полностью mocked:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_llm.py `
  tests\test_strict_api.py tests\test_agent.py
```

Live-тест платный и запускается только явно. Задайте ключ только в текущем PowerShell
process и не сохраняйте его в history или файлах проекта:

```powershell
$env:OPENAI_API_KEY = Read-Host 'OPENAI_API_KEY'
$env:RUN_OPENAI_INTEGRATION = '1'
$env:OPENAI_INTEGRATION_MODEL = 'gpt-6.2-sol'
.\.venv\Scripts\python.exe -m pytest -q tests\test_openai_integration.py -s
Remove-Item Env:OPENAI_API_KEY
Remove-Item Env:RUN_OPENAI_INTEGRATION
Remove-Item Env:OPENAI_INTEGRATION_MODEL
```

Без флага или ключа тест сообщает `SKIPPED`. Для проверки Astra повторите команду с
`OPENAI_INTEGRATION_MODEL=gpt-6.2-astra`. Это отдельные минимальные запросы; массовый
evaluation автоматически не запускается.

## Безопасное обновление публичного стенда и rollback

Только после отдельного разрешения владельца:

1. Зафиксировать текущие SHA, `git status`, PID/command line FastAPI/Caddy, Funnel status,
   read-only `/healthz`, `/readyz` и резервную копию текущего frontend bundle/`.env` вне Git.
2. Выполнить pytest, Ruff, pip check, TypeScript check, production build и diff check на
   релизном SHA. Не запускать live API test без отдельного осознанного разрешения затрат.
3. В существующем `.env` вручную добавить только нужные новые переменные. Ключ вводит
   владелец непосредственно на машине; его значение не печатается и не копируется в Git.
4. Сначала оставить `LLM_DEFAULT_PROVIDER=ollama`, включить OpenAI и allowlist. Собрать
   frontend в подготовительном каталоге, не заменяя работающий bundle.
5. В согласованное окно перезапустить только FastAPI, атомарно заменить bundle, не менять
   Caddy/Funnel/хранилища. Проверить capabilities/readyz и по одному разрешённому
   синтетическому запросу Ollama и OpenAI.
6. Только после успешной проверки при необходимости отдельно изменить default на OpenAI.
7. При ошибке вернуть прежний bundle и прежний `.env`, перезапустить только FastAPI и
   подтвердить Ollama-only `/readyz`. Не очищать PostgreSQL, Qdrant, OpenSearch или volumes.

## Ограничения

- Live OpenAI в реализации CLIP-013 считается непроверенным, пока тест не выполнен с
  ключом конкретного API project.
- Token guard не является финансовой гарантией и не оценивает цену в клиенте.
- Нет production ACL; Caddy Basic Auth остаётся общей защитой demo ingress.
- Корпоративный текст при выборе OpenAI покидает локальный контур. Владелец должен явно
  разрешить такой режим для конкретного стенда и данных.
