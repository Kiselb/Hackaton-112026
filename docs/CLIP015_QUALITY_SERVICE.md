# CLIP-015 — Quality Service и утверждённые экспертные знания

## Граница изменения

CLIP-015 добавляет управляемый маршрут `нет подтверждённого ответа → обращение →
уведомление → экспертное разъяснение → публикация в существующий RAG`. Это пополнение
Knowledge Base, а не fine-tuning. LangGraph, hybrid retrieval, SQL visibility gate,
нумерация citations и fail-closed grounding validation не заменены.

Разработка и автоматические тесты не изменяют действующий `.env`, публичный frontend,
PostgreSQL/Qdrant/OpenSearch стенда и не отправляют реальную почту.

## Модель данных

`quality_requests` хранит UUID, публичный номер `QA-…`, уникальные `trace_id` и
`idempotency_key`, обязательный `conversation_id`, исходный вопрос, outcome/reason,
bounded-контекст уточнений, независимые статусы обращения и e-mail, timestamps и
редактируемый draft. Уникальность trace не допускает второй ticket после другого
нажатия или конкурентного HTTP-запроса.

`quality_knowledge_entries` — неизменяемые содержательные snapshots. Уникальная пара
`(quality_request_id, version)` сохраняет историю; checksum вычисляется по вопросу,
заголовку, ответу, scope и условиям. После утверждения application-level SQLAlchemy
guard запрещает менять содержательные поля. Публикационные поля `status`,
`document_id`, `published_at` изменяются отдельно.

`quality_audit_events` записывает создание, draft, утверждение, отклонение, ошибку
индексации и публикацию, время, механизм авторизации и только безопасную диагностику.
Метка демонстратора не выдаётся за подтверждённую корпоративную identity.

Таблицы additive: startup `Base.metadata.create_all()` создаёт только отсутствующие
таблицы/индексы/constraints. Существующие таблицы не пересоздаются и не очищаются.

## Статусы

Обращение: `awaiting_expert → draft → approved → published`; до утверждения возможно
`rejected`. Опубликованная старая версия остаётся в истории. Исправление сохраняется как
новый draft и новая версия; при успешной публикации нового документа предыдущий
документ этой заявки деактивируется как техническая ревизия, но не удаляется.

E-mail: `not_requested`, `queued`, `sending`, `sent`, `failed`, `delivery_unknown`.
`sent` означает только успешный ответ SMTP transport на `send_message`, а не доставку в
почтовый ящик. Разрыв/timeout с неопределённым результатом даёт `delivery_unknown` и не
разрешает автоматический повтор. Явный retry доступен только для `failed` и требует
права службы качества.

Knowledge entry: `approved → publishing → published` либо `indexing_failed`. Повторная
публикация `published` идемпотентна; retry `indexing_failed` использует тот же Document.

## Создание обращения и idempotency

Кнопка показывается только для `quality_review_required` и `conflict_detected`; перед
POST браузер явно спрашивает подтверждение. Endpoint принимает только `trace_id`,
`Idempotency-Key` и `X-Conversation-Token`: произвольный recipient, subject или текст
письма передать нельзя. Сервер повторно проверяет outcome, принадлежность trace диалогу,
случайный ownership token и TTL. Legacy trace без capability-диалога закрыт fail-closed;
`user_id/session_id` не считаются доказательством владения. Есть глобальный bounded
лимит новых обращений за час.

## SMTP

По умолчанию `QUALITY_EMAIL_ENABLED=false`. TLS использует `ssl.create_default_context()`
с обычной проверкой сертификата; timeout ограничен. From/To берутся только из backend
environment, проверяются на CR/LF и не возвращаются API. Пользовательский ввод попадает
только в plain-text body через `EmailMessage`; полные документы не включаются, длина
ограничена. Пароль — `SecretStr`, не сериализуется в trace/API/audit.

Настройка действующего локального `.env` владельцем (значения не печатать и не
коммитить):

```dotenv
QUALITY_EMAIL_ENABLED=true
QUALITY_EMAIL_TO=<получатель службы качества>
QUALITY_SMTP_HOST=<smtp-host>
QUALITY_SMTP_PORT=587
QUALITY_SMTP_USERNAME=<outgoing-account>
QUALITY_SMTP_PASSWORD=<secret>
QUALITY_SMTP_FROM=<verified-sender>
QUALITY_SMTP_STARTTLS=true
QUALITY_SMTP_TIMEOUT_SECONDS=15
QUALITY_APPROVAL_TOKEN=<отдельный случайный demo-код>
```

SMTP-отправитель не обязан принадлежать домену получателя. Реальный адрес получателя и
credentials отсутствуют в Git. Live smoke выполняется только после отдельного разрешения:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_quality_email.py
# SKIPPED без конфигурации/подтверждения.
.\.venv\Scripts\python.exe scripts\smoke_quality_email.py --confirm-send-one-email
```

Вторая команда отправляет ровно одно минимальное синтетическое письмо.

## Демонстрационная авторизация эксперта

Список и detail обращений, draft, approve, reject, publish и retry-email требуют
`X-Quality-Approval-Token`. Сервер сравнивает его с `QUALITY_APPROVAL_TOKEN` через
`hmac.compare_digest`. Код вводится в UI, живёт только в React state, не встраивается в
bundle и не сохраняется в БД. Это разграничение демонстрационной роли, не SSO, ACL или
проверка личности. Общий Caddy Basic Auth не даёт право утверждать знания.

## Публикация в обычный RAG

После approve формируется отдельный plain-text источник с типом, номером обращения,
номером/датой утверждения, исходным вопросом, дословным экспертным ответом, scope и
условиями. Создаются обычные `Document(active=false,status=indexing)` и `Chunk`, затем
вызывается существующий `index_chunks()` для BGE-M3, Qdrant и OpenSearch.

Только после полного возврата `index_chunks()` PostgreSQL атомарно переводит Document в
`active=true,status=ready`. Если Qdrant успел принять данные, а OpenSearch завершился
ошибкой, Document остаётся невидимым из-за SQL visibility gate. После публикации обычный
`retrieve()` добавляет provenance source card, но не меняет ранжирование и не назначает
экспертному тексту безусловный приоритет над регламентом. Несовместимые активные
источники по-прежнему должны приводить к `conflict_detected`.

Повторный вопрос выполняет BGE-M3/BM25/RRF, SQL gate, существующую генерацию, проверку
номеров `[N]` и semantic grounding. Текст не подставляется напрямую из обращения.

## API

- `POST /api/v1/quality-requests` — ownership token + idempotency key;
- `GET /api/v1/quality-requests` и `GET /{id}` — demo quality token;
- `POST /{id}/draft`, `/approve`, `/reject`, `/publish`, `/retry-email` — demo quality token;
- `GET /api/v1/admin/analytics` — операционные счётчики обращений, SMTP, approve,
  publish и reject; это не метрика правильности ответов.

Размеры всех полей ограничены Pydantic. API не принимает recipient/from и не возвращает
секреты. Invalid transition получает 409, неверное право — 403, несуществующее или чужое
основание — 404 без раскрытия ownership.

## UI и демонстрация полного сценария

1. На синтетическом вопросе без evidence получить `quality_review_required`.
2. Нажать «Обратиться…», подтвердить и увидеть номер/честный SMTP status.
3. Открыть вкладку «Служба качества», ввести выданный владельцем demo-код.
4. Открыть заявку, заполнить ответ, scope, условия и метку демонстратора; сохранить draft.
5. Отдельно утвердить неизменяемую версию, затем опубликовать её в RAG.
6. Убедиться в `published` и наличии `document_id`.
7. Нажать «Повторить вопрос в AI-Агенте»: UI начинает новый диалог.
8. Проверить `answered`, citation `[N]` и карточку «Экспертное разъяснение службы качества»
   с номером заявки, approval и scope.
9. Если текста недостаточно или он конфликтует с регламентом, ожидаем безопасный отказ,
   а не искусственно гарантированный `answered`.

Результат на четырёх реальных регламентах заказчика: `not measured / manual review required`.

## Проверки

`tests/test_quality_service.py` использует SQLite, mocked SMTP и mocked indexing. Он
проверяет ownership/outcome, idempotency, server-only recipient, SMTP success/unknown,
отсутствие секретов, отдельный approval token, immutable snapshot, обычный Document/Chunk,
fail-closed индексацию, retry без дубля и OpenAPI contract. Общий suite сохраняет
регрессии Strict Agent, provider sessions, dialogue, citation и grounding.

Фактический профиль CLIP-015: `10 passed`. Полный suite: `121 passed, 1 skipped`;
skipped — только явно включаемый live OpenAI test. Ruff, `pip check`, TypeScript project
check, Vite production build, `git diff --check`, in-memory additive schema smoke и
OpenAPI contract validation пройдены. Production bundle отдельно открыт в локальном
браузере: вкладка, предупреждение о demo-роли, password input и disabled/enabled
состояние кнопки отрендерились корректно. `npm audit --omit=dev` не запускался: npm
отсутствует в PATH и в bundled Node runtime; зависимости frontend не изменялись. Live
SMTP smoke без подтверждающего флага вернул `SKIPPED`. SMTP, Ollama, OpenAI и production
storage в автоматических тестах не использовались.

## Безопасный rollout

Только после отдельного разрешения владельца:

1. Повторно проверить SHA/status, PID/command line FastAPI/Caddy, Funnel, read-only
   health/ready; сделать verified backup PostgreSQL и копию `.env`/bundle вне Git.
2. Выполнить полный release checklist на зафиксированном SHA. Не запускать live smoke.
3. Read-only проверить отсутствие/структуру трёх `quality_*` таблиц; добавить новые env
   вручную, оставив `QUALITY_EMAIL_ENABLED=false`.
4. В окно работ перезапустить только FastAPI: `create_all()` создаст additive schema.
   Проверить PK/FK/unique/indexes и OpenAPI.
5. Атомарно заменить подготовленный frontend bundle; Caddy/Funnel не менять.
6. Выполнить синтетический сценарий с выключенным e-mail. Затем отдельно настроить SMTP,
   включить его и только с разрешением выполнить одно live письмо.
7. Публикацию первого экспертного источника делать на согласованном синтетическом вопросе,
   проверив Qdrant/OpenSearch и PostgreSQL visibility.

## Rollback и ограничения

Application rollback: вернуть прежние SHA/bundle/`.env`, перезапустить только FastAPI.
Additive-таблицы и созданные документы безопаснее оставить: старая версия их игнорирует,
а удаление уничтожило бы аудит. Физическое удаление — только отдельная операция после
backup; Qdrant/OpenSearch/volumes не очищать. При неудачной публикации SQL уже скрывает
частичные индексные записи; их reconciliation вне scope CLIP-015.

Ограничения: нет production identity/ACL, очереди SMTP, bounce/webhook delivery tracking,
distributed rate limiter и автоматической reconciliation derived indexes. Синхронные
SMTP/индексация подходят для MVP, но не для production high availability. Approval не
устанавливает юридический приоритет источника. OCR и гарантированное распознавание таблиц
не добавлены.
