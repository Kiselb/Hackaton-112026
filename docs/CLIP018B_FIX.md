# CLIP-018B-FIX — Glossary Regression Analysis & Retrieval Isolation

## Решение

**Включать глоссарий на публичном стенде: НЕТ.** Retrieval-only устранил прямую
подмену вопроса агента и восстановил часть поведения, но не прошёл критерии безопасного
включения: correct abstention снизился до `0.0000`, clarification восстановился лишь до
контрольного `0.3333`, technical error rate вырос до `0.0741`, а новые относительно
контроля неподтверждённые outcomes остались. `GLOSSARY_ENABLED=false` сохраняется.

Gold всех 27 кейсов имеет статус `draft`. Автоматические fact/citation checks не являются
экспертной содержательной или юридической оценкой.

## Диагностика исходной регрессии

Предыдущая реализация формировала expanded query в `main.py`, а затем передавала его как
единственное `AgentState.question`. Поэтому этот текст использовался одновременно в
`route_and_plan`, evidence assessment, composition, scope/temporal guards и grounding
validation. Рабочая гипотеза о влиянии planning подтвердилась только частично:

1. Раскрытия действительно меняют dense/BM25 ranking. Это первая точка расхождения у
   большинства затронутых кейсов: B и C имеют одинаковую выдачу, отличную от A.
2. Подмена reasoning-вопроса является отдельной причиной. При одинаковой выдаче B и C
   расходятся на planning/composition/validation; C восстанавливает GLO-001 и GLO-003.
3. Retrieval-only не устраняет последствия изменившегося evidence set. GLO-011,
   GLO-018 и GLO-023 остаются хуже A, хотя reasoning снова получает `resolved_query`.
4. В GLO-003 Passage Recall@5 вырос с `0` до `1`, но B завершился grounding abstention;
   C восстановил grounded answer. В GLO-013 и GLO-017 passage coverage вырос, но
   grounded proxy остался `false`.
5. Fail-closed semantic validation не ослаблялась. GLO-022 даёт один и тот же
   `technical_error` во всех режимах из-за невалидного structured assessment. В C
   аналогичная ошибка дополнительно возникла в GLO-021.

Полная приватная таблица с вопросами, `resolved_query`, `retrieval_query`, matches,
top-K, plan, assessment, validation и calls сохранена в игнорируемом Git файле
`eval/runtime/clip018b_fix_case_level.json`. Старые traces и результаты не изменялись.

## Обезличенная case-level сводка

| Case | Наблюдение | Первая значимая точка | Итог C |
|---|---|---|---|
| GLO-001 | A уточняет, B отвечает без прохождения grounded proxy | retrieval, затем reasoning | C снова уточняет |
| GLO-003 | A grounded; B не публикует draft, хотя passage стал лучше | retrieval, затем validation | grounded восстановлен |
| GLO-011 | A grounded; B/C теряют нужный passage | retrieval ranking | regression остаётся |
| GLO-013 | Passage Recall@5 `0.0 → 0.5`, grounded остаётся false | retrieval ranking | улучшения ответа нет |
| GLO-017 | Passage Recall@5 `0.3333 → 0.6667`, grounded остаётся false | retrieval ranking | улучшения ответа нет |
| GLO-018 | A успешно завершает clarification; B проваливает final validation | retrieval, затем reasoning/validation | clarification восстановлен, grounded нет |
| GLO-021 | A/B безопасно отказывают; C получает invalid structured assessment | evidence assessment | technical error |
| GLO-022 | Одинаковый technical error без применённого раскрытия | evidence assessment | без изменения |
| GLO-023 | A безопасно отказывает; B/C публикуют ответ об отсутствии точного SLA | retrieval ranking | correct abstention regression |
| GLO-002/GLO-005 | Раскрытия и ranking меняются, outcome/grounding одинаковы | retrieval без downstream эффекта | стабильно |

Для GLO-001 фраза «B не уточняет» не считается автоматически улучшением: expected outcome
в draft gold — `answered`, но grounded proxy остаётся false. Для GLO-018 оба режима
начинают clarification, однако только A и C успешно проходят размеченный второй ход.

## Архитектура retrieval-only

```text
original_question ──> dialogue rules ──> resolved_query ──> agent reasoning/validation
                                          │
                                          └─> glossary expansion ──> retrieval_query
                                                                    │
                                                                    └─> dense + BM25 + RRF
```

- `original_question` и `resolved_query` сохраняются без терминологической подмены.
- `glossary_matches` содержит только фактически обнаруженные пары и статус применения.
- `retrieval_query` становится первым поисковым вариантом, но не evidence.
- planning, clarification, assessment, composition и validation используют
  `resolved_query`.
- Multi-Query генерируется из `resolved_query`; первый фактический search использует
  `retrieval_query`, общий `AGENT_MAX_SEARCH_CALLS` не меняется.
- `active=true/status=ready`, citations, grounding, token и call budgets сохранены.
- `GLOSSARY_MODE=legacy_agent_query` нужен только для воспроизводимого B-прогона.
  Default — `retrieval_only`; `GLOSSARY_ENABLED` остаётся `false`.

Глоссарий не передаётся целиком в prompt, не добавляется в sources и не может быть
процитирован как корпоративное доказательство.

## A/B/C на изолированном контуре

Проверены loader checksum `44049beecd1306d21652b100fd2ebb10d710fb4d7071dde33f2db01892c3649e`,
dataset SHA-256 `547db6b8c18fae819146d171d594db6fb55c37338a0893f9b13ca53b96f01639`
и corpus SHA-256 `f23a62712eab8f4aeeae1d49e8e4235020d1f615c22290e77cb3808e9d0861b5`.
Все режимы имеют source-tree SHA-256
`f9898ccae02410a052249f96b3829ea4563f65b6eeb81145732e7c5caea499cb`, Multi-Query OFF,
тот же `qwen3.5:9b`, четыре документа и изолированные ресурсы
`clip017_customer_v1_offline`. Переиндексация не выполнялась.

- A: `clip018b_glossary_customer_v1-20261127T152022+0300-451edcc1-f9898cca`;
- B: `clip018b_glossary_customer_v1-20261127T152255+0300-451edcc1-f9898cca`;
- C1: `clip018b_glossary_customer_v1-20261127T152526+0300-451edcc1-f9898cca`;
- C2 repeat: `clip018b_glossary_customer_v1-20261127T153052+0300-451edcc1-f9898cca`.

| Метрика | A: OFF | B: legacy | C: retrieval-only |
|---|---:|---:|---:|
| Document Recall@3 | 0.9420 | 0.8551 | 0.8551 |
| Passage Recall@5 | 0.6884 | 0.7246 | 0.7246 |
| MRR | 0.7391 | 0.7681 | 0.7681 |
| Grounded answer rate | 0.6957 | 0.5652 | 0.6087 |
| Correct abstention | 0.5000 | 0.2500 | 0.0000 |
| Clarification success | 0.3333 | 0.0000 | 0.3333 |
| Outcome accuracy | 0.8889 | 0.8148 | 0.8148 |
| Citation ID validity | 0.9259 | 0.9630 | 0.8889 |
| Citation support | 0.7197 | 0.5985 | 0.6742 |
| Technical error rate | 0.0370 | 0.0370 | 0.0741 |
| Search calls | 28 | 28 | 28 |
| LLM calls | 77 | 79 | 76 |
| Input / output tokens | 243078 / 7985 | 253874 / 9071 | 241791 / 7940 |
| Latency p50, ms | 4518 | 4771 | 4586 |
| Latency p95, ms | 6663 | 6628 | 6766 |

C2 в точности повторил все перечисленные quality/call/token метрики и все case-level
outcomes C1; изменились только latency p50/p95 (`4550/7532 ms`). Это подтверждает
воспроизводимость наблюдаемого результата на двух последовательных C-прогонах, но не
делает малую draft-выборку статистическим доказательством.

В обоих glossary-режимах найдено 43 вхождения: 38 применены и 5 консервативно
пропущены; дополнительных retrieval calls нет. Вариант D не запускался: C уже не прошёл
safety-gates, а добавление ещё одного original/expanded search изменило бы cost profile и
не устранило установленную проблему evidence/outcome автоматически.

## Abstention, clarification и неподтверждённые ответы

Denominator correct abstention — 4 кейса, clarification — 3. В C clarification вернулся
к A (`1/3`), но безопасные отказы ухудшились до `0/4`. GLO-023 остаётся `answered` вместо
контрольного `quality_review_required`; GLO-021 стал `technical_error`. Относительно A
появилась также grounded regression в GLO-018. Поэтому требование «нет новых
неподтверждённых ответов» не выполнено. Regex-проверка forbidden claims при этом не нашла
новых запрещённых формулировок; это не компенсирует провал outcome/grounding gates.

## Тесты и ограничения

Добавлены регрессии на неизменность reasoning question, отдельный retrieval query,
несколько сокращений, ambiguity/`по`, отсутствие glossary evidence, dialogue boundary,
Multi-Query budgets и legacy reproduction. Остальные обязательные сценарии покрываются
существующими `test_agent.py`, `test_conversations.py`, `test_quality_service.py` и
`test_strict_api.py`.

Фактически выполнено:

- целевой набор `test_glossary.py + test_agent.py + test_conversations.py`:
  `77 passed`;
- полный `pytest`: `200 passed, 1 skipped` (две существующие FastAPI deprecation warnings);
- `ruff check backend tests scripts`: passed;
- `python -m pip check`: `No broken requirements found`;
- `git diff --check`: passed; отображаются только предупреждения Git о будущей
  нормализации LF/CRLF;
- `npm run build` и `npm audit --omit=dev`: not run — `npm` отсутствует в `PATH`.

Frontend dependencies и lockfile не устанавливались и не изменялись. Browser-проверка
не выполнялась; backend/API-контракт для UI не менялся.

Ограничения: draft gold, малая выборка, автоматические lexical proxies, отсутствие
экспертного review и один устойчивый pre-existing technical-error кейс. Измерения не
обосновывают deployment.

## Безопасный deployment и rollback

Deployment сейчас запрещён. Для будущего повторного рассмотрения нужны expert review
GLO-011/GLO-018/GLO-021/GLO-023, исправление retrieval policy на потерянных passages и
новый сопоставимый эксперимент без изменения gold/evaluator.

Если владелец позднее отдельно разрешит rollout: проверить Git SHA, checksum, backup и
read-only readiness; оставить `GLOSSARY_MODE=retrieval_only`, включить только
`GLOSSARY_ENABLED=true`, перезапустить только FastAPI и выполнить разрешённый smoke.
Rollback — вернуть `GLOSSARY_ENABLED=false` и перезапустить только FastAPI. Миграции,
reindex, Caddy/Funnel, документы и поисковые ресурсы для rollout/rollback не требуются.
