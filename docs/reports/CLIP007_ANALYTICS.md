# CLIP-007 — Search Analytics, Agent Trajectories & Demo UI

Дата: 2026-11-23. Исходная версия: `791b660` (CLIP-006).

## Решение

Существующий RAG и LangGraph не изменялись. Поверх PostgreSQL `SearchTrace` добавлены
детерминированная аналитика, bounded read API, viewer фактических trajectory и связанный
с trace feedback. React UI теперь имеет четыре раздела: поиск, аналитика, traces и
документы.

Dashboard показывает реальные записи текущей PostgreSQL базы: число запросов,
технические ошибки, обоснованные отказы, случаи ручной проверки, mean/nearest-rank p95
latency, LLM calls, input/output tokens, baseline/agent share, fallback и дневную
динамику. Пустая история возвращает нули и явные `not_measured`, а не выдуманные
измерения.

## Классификация

Категории пересекаются. Техническое выполнение и содержательный результат разделены:

| Категория | Детерминированный сигнал |
|---|---|
| `technical_error` | trace status отличается от `ok`, включая empty/incomplete execution |
| `no_evidence` | технически успешный retrieval вернул 0 либо записан `refusal_reason=no_evidence` |
| `evidence_gap` | сохранены gaps, известный evidence-based refusal или feedback `missing_documents` |
| `justified_abstention` | один из известных отказов: no evidence, temporal gap, explicit source gap, failed validation |
| `citation_problem` | есть выбранные источники и persisted citation validation=false |
| `agent_fallback` | agent runtime завершился fallback в baseline |
| `manual_review_required` | no evidence/gap/citation/fallback либо негативный feedback |
| `answer_produced` | есть непустой не-refusal ответ при техническом успехе |

`status=ok` не трактуется как правильный ответ. Для обычного ответа quality status равен
`unknown`; общая содержательная корректность Dashboard — `not_measured`. Feedback
`helpful` также не переводит ответ в «истинный».

`no_evidence` означает только, что выбранный retrieval не дал достаточных доказательств
в активных документах. UI и классификатор не утверждают отсутствие информации во всём
корпоративном каталоге.

## API

- `GET /api/v1/capabilities` возвращает фактический default mode и доступность demo
  override.
- `GET /api/v1/admin/analytics` принимает опциональные `user_id`, `session_id`,
  `created_after`, `created_before` и возвращает агрегаты и `time_series`.
- `GET /api/v1/admin/traces` сохраняет совместимый JSON-list, принимает `limit` 1–100,
  `offset`, `status`, `mode`, `category`, identity/time filters и возвращает общий размер
  в `X-Total-Count`.
- `GET /api/v1/admin/traces/{trace_id}` возвращает ответ, категории, feedback,
  безопасную проекцию details и доказательства в исходном citation order.
- `POST /api/v1/traces/{trace_id}/feedback` принимает одну из оценок `helpful`,
  `incomplete`, `incorrect`, `missing_documents` и требует совпадения `user_id` и
  `session_id` с trace.

Необработанные exception/fallback strings не передаются клиенту: публичная проекция
сохраняет факт и безопасный код, а полная диагностическая строка остаётся локально в
PostgreSQL.

## Feedback и схема данных

Добавлена только таблица `trace_feedback`: UUID, FK `trace_id`, `user_id`, `session_id`,
rating, timestamp. Это additive-изменение: существующий startup `create_all` создаёт
отсутствующую таблицу и не пересоздаёт `search_traces`, индексы или данные.

Feedback предназначен для последующего анализа. Он не меняет документы, search indexes,
ответ или agent memory и не является подтверждением корпоративного факта. Совпадение
identity ограничивает случайную запись оценки в чужой trace, но не заменяет production
authentication/ACL.

## Интерфейс

- `[N]` в ответе — ссылка на соответствующую карточку источника;
- карточка показывает title, filename и page только при наличии page в PostgreSQL;
- child search chunk вынесен в evidence excerpt, parent context раскрывается и содержит
  точную подсветку по сохранённым offsets;
- ответ показывает фактический baseline/agent mode, latency и token counts;
- trace viewer выводит route chronologically: node, decision, latency, tokens и
  раскрываемые details, queries, gaps, validation, fallback/errors и evidence;
- baseline trace получает отдельное явное сообщение, что LangGraph не запускался;
- analytics содержит предупреждение, что технический успех не равен корректности.

Сложная graph-библиотека в frontend не добавлялась; trajectory отображается обычной
хронологической лентой React/CSS.

## Локальная демонстрация agent mode

Безопасный default остаётся:

```dotenv
AGENT_ENABLED=false
AGENT_DEMO_OVERRIDE_ENABLED=false
```

Для loopback-демо установить только `AGENT_DEMO_OVERRIDE_ENABLED=true` и перезапустить
backend. UI покажет baseline/agent selector, а `/api/v1/ask` применит выбор только к
этому запросу. При выключенном флаге поле `mode` даёт HTTP 403. Глобальные настройки
через UI не меняются. Если agent runtime падает и выполняется baseline fallback, ответ
помечается baseline, а fallback сохраняется для аналитики.

## Проверки

- `pytest -q`: 45 passed; два существующих FastAPI `on_event` warnings;
- `ruff check backend tests scripts`: passed;
- `npm run build`: passed, 29 modules;
- `pip check`: no broken requirements;
- `npm audit --omit=dev`: 0 vulnerabilities;
- реальный Browser QA: search, dashboard, trace filters/detail, feedback и documents
  navigation открываются; console errors/warnings отсутствуют;
- baseline browser query: 12 кликабельных citation occurrences, 8 evidence cards,
  переход к `#source-1` и раскрытие parent context прошли;
- agent browser query при process-only demo override: фактические nodes
  `route_and_plan`, `retrieve`, `assess_evidence`, `compose_answer`,
  `validate_answer`, `finish`, подзапросы и validation отображены;
- mobile viewport 390×844: horizontal overflow отсутствует (`scrollWidth=clientWidth`).

Browser QA создал две обычные записи `SearchTrace` для `web-demo/browser` (baseline и
agent) и один feedback `helpful` для baseline trace в проектной PostgreSQL. Документы,
Qdrant и OpenSearch не изменялись. Временные backend/frontend процессы и каталог логов
после проверки остановлены и удалены.

Unit/API-level tests покрывают агрегаты и пустую историю, разделение technical/quality,
пересекающуюся классификацию, nearest-rank p95, mode share/tokens/LLM calls,
фильтрацию и пагинацию, redaction исключений, запись feedback с identity check,
восстановление evidence и различение baseline/agent trajectory.

Retrieval/answer-quality baseline повторно не запускался: конвейер RAG, prompt, модели и
параметры поиска не менялись. Новые quality score не заявляются; semantic correctness
остаётся `not_measured`.

## Ограничения

- административные endpoint'ы не защищены аутентификацией; приложение допустимо только
  на `127.0.0.1` с синтетическими или разрешёнными данными;
- identity filters и feedback identity check не являются ACL;
- автоматического LLM Judge и semantic correctness score нет;
- старые baseline traces могут не содержать точного `llm_calls`; для них используется
  консервативный вывод по token/citation-repair полям;
- дневная динамика и in-process агрегация рассчитаны на MVP-объём; отдельная
  аналитическая БД не добавлялась;
- OCR, таблицы и production security остаются вне scope.
