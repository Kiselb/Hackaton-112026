# CLIP-DEMO-UX-002 — многовопросный demo-диалог

## Фактическое поведение

`GET /api/v1/capabilities` остаётся источником фактического режима для frontend:
`default_mode=agent` возвращается только при `AGENT_ENABLED=true`. Demo override лишь
разрешает request-level переключатель и не меняет default самостоятельно.

Диалог AI-Агента принимает новый вопрос в статусе `completed` с тем же
`conversation_id` и ownership token. Каждый вопрос получает собственные user/assistant
turns, `trace_id`, sources и metrics. Новый question-cycle сбрасывает только clarification
context и счётчики предыдущего вопроса. `closed`, `expired` и `processing` не принимают
новый ход.

Ссылочный вопрос получает не более `CONVERSATION_MAX_CONTEXT_CHARS` символов последнего
question-cycle. Этот текст явно помечен как контекст, а не доказательство; retrieval,
PostgreSQL visibility, citations и grounding выполняются заново. Неоднозначная ссылка
возвращает `clarification_required`.

## Получение `default_mode=agent`

Текущий локальный `.env` до развёртывания необходимо изменить владельцу вручную; файл не
коммитится. В согласованное окно установить ровно:

```dotenv
AGENT_ENABLED=true
AGENT_DEMO_OVERRIDE_ENABLED=true
MULTI_QUERY_DEFAULT_ENABLED=false
```

Не менять DSN, имена PostgreSQL/Qdrant/OpenSearch, Ollama или proxy. После ручного
перезапуска только FastAPI штатным способом владельца проверить loopback-контракт:

```powershell
$capabilities = Invoke-RestMethod http://127.0.0.1:8000/api/v1/capabilities
$capabilities | ConvertTo-Json -Depth 5
if ($capabilities.default_mode -ne 'agent') { throw 'default_mode is not agent' }
if (-not $capabilities.agent_demo_override_enabled) { throw 'demo override is disabled' }
if ($capabilities.multi_query.default_enabled) { throw 'Multi-Query default must be false' }
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:8000/readyz
```

## Безопасное обновление существующего Windows-стенда

1. Зафиксировать текущие Git SHA, PID/командную строку FastAPI, Caddy и Funnel, результаты
   `/healthz`, `/readyz` и `/api/v1/capabilities`. Скопировать `.env` и текущий
   `frontend/dist` в доступный только владельцу backup-каталог вне Git.
2. На целевом commit выполнить Python 3.12 tests, Ruff, `pip check`, frontend build и
   production dependency audit. Сервисы и рабочий bundle во время этих проверок не менять.
3. Вручную вне Git изменить только три значения выше. Собрать новый `frontend/dist` в
   подготовительном каталоге и сверить его с тем же Git SHA.
4. В согласованное окно перезапустить только FastAPI штатной командой/службой владельца и
   атомарно заменить Caddy static bundle. Не останавливать и не перенастраивать Caddy,
   Tailscale Funnel, PostgreSQL, Qdrant, OpenSearch или Ollama.
5. Повторить read-only проверки выше через loopback и защищённый публичный URL. Затем в
   одной вкладке выполнить разрешённый синтетический сценарий: вопрос → ответ → второй
   вопрос → ответ → reload → третий вопрос; проверить неизменный `conversation_id`, разные
   traces и доступность прежних sources/feedback.
6. При ошибке вернуть backup bundle и прежние три значения `.env`, после чего
   перезапустить только FastAPI. Таблицы, документы, индексы и volumes не очищать.

Автоматическое развёртывание, `git push`, `Stop-PublicDemo.ps1` и перезапуск публичных
компонентов в рамках задачи не выполняются.
