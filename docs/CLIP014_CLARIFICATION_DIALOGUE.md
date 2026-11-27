# CLIP-014 — Stateful Clarification Dialogue

## Граница изменения

CLIP-014 расширяет существующий bounded LangGraph, не создаёт второго агента и не меняет
retrieval, SQL visibility gate, citation checks или semantic grounding CLIP-012-FIX.
Каждый пользовательский ход получает новый `SearchTrace` и новый request-scoped
`LlmSession`. Рабочие процессы, `.env`, Caddy, Tailscale Funnel и реальные данные стенда
в рамках разработки не изменялись.

## Модель хранения

Добавлены две таблицы, создаваемые существующим `Base.metadata.create_all()`:

- `conversations`: UUID, SHA-256-хеш ownership token, демонстрационный owner/session ID,
  статус, режим, исходный вопрос, текущий вопрос уточнения, bounded JSON-контекст,
  счётчики, version, timestamps и expiry;
- `conversation_turns`: UUID, conversation FK, уникальный ordinal, role/kind, текст,
  outcome, provider/model и nullable FK на `search_traces`.

Существующие таблицы не получают новых колонок и не пересоздаются. `SearchTrace.details`
содержит только несекретные `conversation_id`, ordinal, исходный вопрос, resolved query,
счётчики и outcome; он не является единственным хранилищем состояния.

Статусы: `processing`, `awaiting_clarification`, `completed`, `closed`, `expired`.
`processing` — короткая серверная блокировка бизнес-состояния между принятием сообщения и
завершением его обработки.

## Жизненный цикл и bounded workflow

1. Самостоятельный agent-вопрос проходит обычный input guard.
2. Сервер создаёт диалог, случайный 256-bit-class token и user turn, затем запускает
   существующий LangGraph.
3. При `clarification_required` сохраняется assistant turn и статус
   `awaiting_clarification`.
4. Follow-up сначала проверяет token, TTL, mode и статус под `SELECT ... FOR UPDATE`,
   атомарно переводит состояние в `processing`, после чего выполняет новый LangGraph run.
5. При достаточных доказательствах strict validation публикует `answered`; при частичном
   уточнении возможен ещё один конкретный вопрос; при исчерпании лимита возвращается
   `quality_review_required`.
6. «Не знаю» завершает диалог контролируемой эскалацией без LLM-вызова. Close endpoint
   переводит диалог в `closed` также без LLM.
7. После `completed` следующий вопрос начинает новый bounded question-cycle в том же
   диалоге: история сохраняется, а clarification context и его счётчики сбрасываются.
8. Истёкший или закрытый диалог не продолжается.

Defaults конфигурируются в `.env.example`:

| Параметр | Default |
|---|---:|
| `CONVERSATION_TTL_MINUTES` | 60 |
| `CONVERSATION_MAX_CLARIFICATIONS` | 2 |
| `CONVERSATION_MAX_USER_ANSWERS` | 3 |
| `CONVERSATION_MAX_CONTEXT_CHARS` | 2000 |
| `CONVERSATION_MAX_MESSAGE_CHARS` | 1000 |

## Доступ и изоляция

Первый ответ возвращает token в HTTP-заголовке `X-Conversation-Token`. React хранит token,
`conversation_id`, уникальные `user_id` и `session_id` только в `sessionStorage` вкладки;
секрет не попадает в URL. В PostgreSQL сохраняется только SHA-256-хеш, который сравнивается
через `hmac.compare_digest`. Неверный token получает одинаковый `404`, поэтому endpoint
не подтверждает существование чужого UUID.

Client `user_id` не считается доказательством личности. Token защищает только
демонстрационное состояние; общий Caddy Basic Auth остаётся ingress-защитой, а
production-аутентификация/ACL не реализованы.

## Контекст, retrieval и guardrails

Короткие ответы («Пресейл», «Да», «Дирекция ИБ») проверяются отдельным
`assess_clarification_answer`: standalone question intent не требуется, но сохраняются
проверки языка, бессмысленного ввода, role override и prompt injection. Новый вопрос с `?`
во время ожидаемого уточнения не подставляется в старый контекст: сначала требуется
ответить на уточнение либо явно начать новый диалог.

`resolved_query` сохраняет исходный вопрос и добавляет только bounded список явно
сообщённых пользовательских условий с маркировкой «не корпоративные факты». Он не
заменяет evidence. Новый ход выполняет retrieval заново; Qdrant/OpenSearch hits снова
проходят PostgreSQL `active=true/status=ready`, после чего действуют assessment,
citation IDs и grounding validation. Сохранённые ранее source IDs не передаются как
основание ответа. Поэтому деактивация документа между ходами исключает его из нового
подтверждения.

После окончательного ответа новый самостоятельный вопрос получает turn `new_question`.
Для ссылочной формулировки используется только последний question-cycle, не вся история.
Предыдущий ответ маркируется как контекст разрешения ссылки и не является evidence; новый
ответ всегда требует свежего retrieval. Если ссылка неоднозначна (например, в предыдущем
вопросе названы два документа), backend запрашивает объект ссылки, а не угадывает его.

Уточнение не назначает приоритет конфликтующим документам. Сохранившийся конфликт
возвращает `conflict_detected`; недостаток evidence или исчерпанный лимит —
`quality_review_required`, совместимый с будущим CLIP-015.

## API

Первый ход:

```http
POST /api/v1/ask
Content-Type: application/json

{"question":"Кто назначает руководителя проекта?","mode":"agent"}
```

При неоднозначности прежние поля ответа сохраняются, дополнительно возвращаются
`conversation_id`, `turn_id`, `conversation_status`, `awaiting_clarification`,
`expires_at`; token находится только в response header.

Продолжение:

```http
POST /api/v1/ask
X-Conversation-Token: <secret>
Content-Type: application/json

{"question":"Дирекция ИБ, реализация проекта","conversation_id":"<uuid>"}
```

История и завершение:

```http
GET /api/v1/conversations/<uuid>
X-Conversation-Token: <secret>

POST /api/v1/conversations/<uuid>/close
X-Conversation-Token: <secret>
```

Публичного list-all endpoint нет. Baseline с `conversation_id` получает `409`; скрытого
fallback Agent → Baseline и provider fallback нет.

## LlmSession, traces и аналитика

Все LLM-вызовы одного хода используют одну allowlisted пару provider/model. Явная смена
между ходами записывается только в новый turn/trace. Прошлые записи не изменяются.

Аналитика дополнительно считает distinct conversations, пользовательские сообщения,
уточнения, подтверждённые ответы и переходы в `quality_review_required`. Это operational
счётчики, а не измерение фактической правильности. Trace viewer показывает UUID диалога и
номер хода, но не token и не открывает историю без ownership proof.

## React-интерфейс

Поиск отображает последовательность user/assistant сообщений. Уточнение выделено,
follow-up отправляется той же формой, источники и feedback остаются привязаны к trace
конкретного ответа. История восстанавливается после reload через защищённый GET. Доступны
«Новый диалог» и «Завершить»; после `completed` кнопка снова активна. Режим существующего
диалога остаётся agent и не переключается незаметно между ходами.
Client блокирует повторный submit, а backend дополнительно отклоняет конкурентный ход в
статусе `processing`.

## Тестирование

Изолированные тесты используют SQLite, synthetic evidence и mocked LLM; PostgreSQL,
Qdrant, OpenSearch, Ollama и платный OpenAI не вызываются.

- профиль CLIP-014: `9 passed`;
- полный pytest: `111 passed, 1 skipped` (live OpenAI test skipped), две существующие
  FastAPI `on_event` warnings и одна локальная pytest-cache permission warning;
- Ruff: passed;
- `pip check`: passed;
- TypeScript project check: passed;
- Vite production build: passed, 29 modules;
- additive schema smoke: обе таблицы, 5 indexes, 2 foreign keys и unique ordinal созданы
  в изолированной in-memory БД;
- `git diff --check`: passed, только информационные LF/CRLF warnings;
- `npm audit --omit=dev`: не выполнен — `npm` отсутствует в PATH доступного процесса;
  зависимости frontend не изменялись.

Покрыты creation/ownership hash, follow-up с коротким ответом, повторный resolved query,
второе уточнение и limit, «не знаю», injection, неверный token, history isolation, TTL,
close, completed/busy state и смена темы. Прежние CLIP-012-FIX/013 тесты входят в полный
прогон. Реальный paid API и живой корпус не запускались.

## Ручные демонстрационные сценарии

Названия и факты четырёх документов заказчика не хранятся в Git, поэтому сценарии ниже —
шаблон `manual_review_required`, а не заявленные результаты:

1. Регламент A/B: задать общий вопрос о роли, добиться выбора подразделения/этапа, затем
   проверить, что итог цитирует только применимый активный документ.
2. Регламент B/C: дать частичный ответ, получить второе конкретное уточнение, затем
   проверить подтверждённый ответ или безопасную эскалацию.
3. Регламент C/D: ответить «не знаю» и проверить `quality_review_required` без нового
   LLM-вызова.
4. Два несовместимых положения из любых двух регламентов: уточнить область и убедиться,
   что остаётся `conflict_detected`, без выдуманного приоритета.

Перед демонстрацией владелец должен подставить фактические названия и ожидаемые clauses,
не добавляя документы или результаты в Git. Без выполнения на живом корпусе статус всех
четырёх сценариев: `not measured / manual_review_required`.

## Безопасное применение схемы и стенда

CLIP-014 не применялся к стенду. После отдельного разрешения владельца:

1. Проверить SHA/status, фактические PID/commands, Funnel/Caddy и read-only health/ready;
   создать verified backup PostgreSQL и копию текущих `.env`/frontend bundle вне Git.
2. Выполнить pytest, Ruff, pip check, TypeScript/build и diff check на релизном SHA.
3. Read-only preflight PostgreSQL:

   ```sql
   SELECT table_name FROM information_schema.tables
   WHERE table_schema='public'
     AND table_name IN ('conversations','conversation_turns');
   ```

4. В существующий `.env` вручную добавить только нужные `CONVERSATION_*`; секреты и
   ключи не менять. В согласованное окно перезапустить только FastAPI. Startup
   `create_all()` создаст две отсутствующие таблицы; существующие таблицы не изменит.
5. Проверить наличие PK/FK/unique/indexes в `information_schema`, затем capabilities и
   один разрешённый synthetic dialogue. Только после этого атомарно заменить bundle.
6. Caddy, Funnel, документы, Qdrant/OpenSearch и существующие traces не изменять.

Rollback приложения: вернуть прежний SHA/bundle и `.env`, перезапустить только FastAPI.
Additive-таблицы безопаснее оставить: старая версия их игнорирует. Если владелец отдельно
разрешит физический rollback после backup/export и проверки отсутствия нужных данных,
сначала удалить `conversation_turns`, затем `conversations`; не использовать `DROP` как
часть обычного application rollback. Qdrant/OpenSearch не переиндексировать.

## Ограничения и CLIP-015

- нет production identity, ACL, cross-device sync или recovery потерянного token;
- SHA-256 подходит здесь только потому, что token случайный и высокоэнтропийный;
- crash после `processing` оставляет диалог занятым до TTL; отдельный reconciler не входит
  в scope MVP;
- эвристика новой темы и guardrails допускают false positive/negative;
- correctness на документах заказчика не измерена.

CLIP-015 может использовать `conversation_id`, исходный вопрос, turns и outcome
`quality_review_required` для создания обращения. Ни turn, ни feedback, ни ответ модели
автоматически не становятся корпоративным фактом.
