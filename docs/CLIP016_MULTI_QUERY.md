# CLIP-016 — Multi-Query Retrieval

## Граница изменения

CLIP-016 добавляет управляемое расширение поиска в существующий bounded LangGraph.
Это не второй агент, не новая база знаний и не ослабление strict grounding. При
`multi_query_enabled=false` сохраняется поведение CLIP-015: простой вопрос идёт через
fast route, сложный вопрос может получить до двух planner-запросов, а evidence gap —
один targeted retry.

Не изменяются SMTP, Quality Service, approval token, действующий `.env`, корпоративные
документы, публичный стенд, Caddy/Funnel и ресурсы PostgreSQL/Qdrant/OpenSearch.

## Алгоритм

1. API разрешает Multi-Query только для AI-Агента и только если серверный флаг включён.
2. Для нового хода CLIP-014 сначала строится актуальный `resolved_query`. Именно он,
   а не прежние варианты или source IDs, передаётся в граф.
3. `route_and_plan` выполняет один структурированный LLM-вызов. Для сложного вопроса
   этот же вызов остаётся обычным планировщиком; для простого вопроса он вызывается
   только при запрошенном Multi-Query и достаточном LLM-бюджете.
4. Исходный вопрос всегда ставится первым. Пустые, длиннее 500 символов, повторные и
   практически идентичные варианты отбрасываются. Явные аббревиатуры, числа/даты,
   quoted terms и организационные ограничения нельзя потерять; новое подразделение
   нельзя ввести программно или инструкцией prompt.
5. Каждая формулировка вызывает существующий `search.retrieve()`: BGE-M3, BM25,
   внутренний dense/sparse RRF, PostgreSQL visibility gate и опциональный reranker.
6. Ранжированные списки объединяются вторым, межзапросным RRF с `k=60`. Сырые BM25 и
   cosine scores не складываются. `chunk_id` встречается в evidence один раз, но
   сохраняет список `query_contributions` с формулировкой, rank и RRF contribution.
7. Итоговый evidence проходит прежние assessment, conflict/scope/temporal guards,
   citation validation и grounding validation. Query expansion не является evidence.

Стабильность порядка задаётся `(-multi_query_rrf_score, chunk_id)`. Это обеспечивает
воспроизводимую нумерацию `[1]`, `[2]` при одинаковых входных рангах.

## Лимиты и fail-closed

Defaults:

```dotenv
MULTI_QUERY_ENABLED=true
MULTI_QUERY_DEFAULT_ENABLED=false
MULTI_QUERY_MAX_VARIANTS=2
AGENT_MAX_SEARCH_CALLS=3
AGENT_MAX_RETRIES=1
AGENT_MAX_LLM_CALLS=4
```

`MULTI_QUERY_MAX_VARIANTS` включает оригинал. При defaults начальный этап использует
не более двух retrieval-вызовов и резервирует третий для targeted retry. Multi-Query
не увеличивает глобальные лимиты.

Для простого вопроса expansion запускается, только если остаётся четыре LLM-вызова:
planning/expansion, evidence assessment, answer composition и grounding validation.
Если бюджета меньше, выполняется исходный retrieval и записывается
`skipped_llm_budget / grounding_validation_budget_reserved`. Citation repair возможен
только в остатке общего бюджета; validator не отключается.

Ошибки обрабатываются так:

- invalid/empty/duplicate expansion → `expansion_unavailable`, исходный retrieval;
- ошибка одного retrieval → `partial_retrieval`, успешный список остаётся доступен;
- превышение search budget → лишний вызов не запускается;
- ошибка выбранного LLM-провайдера → прежняя fail-closed политика без provider fallback;
- assessor/validator unavailable или invalid JSON → `technical_error`, draft не публикуется.

## Visibility и provenance

Каждый per-query список формируется только после SQL-фильтра
`Document.active=true AND Document.status='ready'`. Опубликованное экспертное
разъяснение CLIP-015 участвует как `source_type=quality_clarification` и сохраняет
`quality_request_number`, `approval_number`, document/chunk IDs. Draft, approved-but-not-
published, indexing, failed и inactive знания не становятся evidence.

Prompt-injection guard, parent/child provenance и отсутствие выдуманных page/offset
остаются прежними.

## API и UI

`POST /api/v1/ask` принимает необязательное поле:

```json
{
  "question": "Кто назначает руководителя проекта?",
  "multi_query_enabled": true
}
```

Старый клиент без поля работает, default — `false`. Явный `true` для Baseline или при
`MULTI_QUERY_ENABLED=false` получает HTTP 409; режим не игнорируется молча.

`GET /api/v1/capabilities` возвращает:

```json
{
  "multi_query": {
    "available": true,
    "default_enabled": false,
    "max_variants": 2
  }
}
```

В разделе «Поиск» переключатель «Multi-Query — расширенный поиск» действует на каждый
отправляемый ход. В Baseline он disabled с пояснением. После ответа UI показывает
фактический status, число формулировок/retrieval-вызовов/уникальных источников,
Multi-Query latency и общий processing time. Формулировки находятся в раскрываемом
блоке «Как выполнялся поиск».

Provider/model выбираются существующим `LlmSession`; отдельного OpenAI-клиента и
автоматического переключения нет.

## Уточняющий диалог

Каждый follow-up имеет собственное значение переключателя. После ответа пользователя
CLIP-014 строит bounded `resolved_query`, содержащий исходный вопрос и явно введённые
условия. Query expansion применяется заново к этому тексту. Варианты и source IDs
предыдущего хода не переносятся как факты или доказательства.

## Traces и аналитика

В `SearchTrace.details` записываются:

- `multi_query_requested`, `multi_query_effective`, `multi_query_status`;
- `multi_query_queries`, `multi_query_generated_count`, `multi_query_executed_count`;
- `multi_query_search_calls`, `multi_query_unique_sources`;
- `multi_query_deduplicated_count`, `multi_query_latency_ms`;
- `multi_query_fallback_reason`;
- `retrieved_chunks_total` и `retrieved_unique_chunks` отдельно;
- общие `search_calls`, `llm_calls`, tokens, outcome и validation.

Старые traces не требуют миграции: отсутствующие поля трактуются как OFF/0. Analytics
показывает requested/OFF, успешные expansions, fallbacks и группы фактически ON/OFF с
retrieval calls, average/p95 latency, unique sources, tokens, outcomes и errors.
`answered_share` не называется accuracy.

## Оценка

Версионированный набор
`eval/datasets/multi_query_synthetic_v1.jsonl` не содержит корпоративных документов.
Каждый case задаёт вопрос, expected source IDs, scope, outcome, complexity, ambiguity
reason и синтетические ранжированные выдачи. Детерминированный запуск:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_multi_query_synthetic.py
```

Фактический результат на 4 synthetic fixtures:

| Метрика | OFF | ON | Delta |
|---|---:|---:|---:|
| Recall@3 | 0.8750 | 1.0000 | +0.1250 |
| Precision@3 | 0.3333 | 0.4167 | +0.0834 |
| MRR | 0.8750 | 1.0000 | +0.1250 |
| Avg unique sources | 3.00 | 3.75 | +0.75 |
| Synthetic latency, avg ms | 42.5 | 87.0 | +44.5 |
| LLM calls, total | 12 | 16 | +4 |
| Synthetic tokens, total | 2490 | 3150 | +660 |

Latency/tokens здесь — фиксированные cost fixtures для проверки отчёта, не benchmark
железа, Ollama или production. Outcome/citation/grounding flags также synthetic fixtures,
а не доказательство качества модели. Результат на реальных регламентах заказчика:
`not measured / manual_review_required`.

Для реального изолированного golden set существующий evaluator теперь поддерживает
сопоставимые запуски:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval\datasets\corporate_v2.jsonl
.\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval\datasets\corporate_v2.jsonl --multi-query
```

Запуск требует локальных сервисов и отдельного evaluation schema/collections/index;
автоматически в CLIP-016 он не выполнялся.

## Проверки и ограничения

Профильные unit/API tests используют mocked LLM, synthetic evidence и SQLite/FakeDb.
Они покрывают fast-route expansion, оригинал первым, duplicate/scope filtering, budgets,
RRF, partial retrieval, baseline rejection, capabilities, analytics и воспроизводимость
synthetic evaluation. Фактически пройдено: профиль `54 passed`; полный suite
`134 passed, 1 skipped` (skip — только opt-in live OpenAI integration); Ruff, `pip check`,
TypeScript project check, Vite production build, OpenAPI contract и `git diff --check` —
успешно. UI отдельно проверен в локальном браузере с временной SQLite-конфигурацией:
Baseline disabled-state и включение в режиме AI-Агента отработали. `npm audit --omit=dev`
не выполнен: npm отсутствует в PATH и bundled runtime; frontend dependencies не менялись.
Live Ollama/OpenAI/SMTP и production storage не использовались.

Ограничения: semantic equivalence нельзя доказать одними lexical guards; итог зависит
от выбранной LLM и retrieval. Multi-Query способен ухудшить precision, увеличить latency
и обнаружить конфликт вместо `answered` — это корректный результат. Нет production ACL,
OCR, гарантированного чтения таблиц или доказанной accuracy на данных заказчика.

## Безопасное развёртывание и rollback

Развёртывание — только после отдельного разрешения владельца:

1. Зафиксировать SHA/status, проверить процессы и read-only health/ready, сделать verified
   backup PostgreSQL и копии действующих `.env`/frontend bundle вне Git.
2. Выполнить полный release checklist на Python 3.12 без live OpenAI/SMTP/Ollama smoke.
3. Вручную добавить три `MULTI_QUERY_*` значения в существующий `.env`, сначала оставив
   `MULTI_QUERY_DEFAULT_ENABLED=false`.
4. Перезапустить только FastAPI; миграций и переиндексации нет. Read-only проверить
   capabilities/OpenAPI и синтетический mock-сценарий.
5. Атомарно заменить проверенный frontend bundle. Caddy/Funnel не менять.

Rollback: вернуть предыдущий SHA/bundle и прежний `.env`, перезапустить только FastAPI.
Новых таблиц/индексов нет; traces с дополнительными JSON-полями старая версия безопасно
игнорирует. Не очищать БД, Qdrant, OpenSearch или Docker volumes.
