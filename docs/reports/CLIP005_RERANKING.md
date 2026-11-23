# CLIP-005 — Reranking & Hard-Negative Evaluation

Дата: 2026-11-23. Исходная версия: f5b2408. Все модели и сервисы локальные.

## Решение

BAAI/bge-reranker-v2-m3 реализован через sentence_transformers.CrossEncoder, но
RERANK_ENABLED=false сохранён по умолчанию. На corporate_v2 reranker улучшил порядок
первых документов, однако не улучшил итоговые факты и отказы и добавил около
3,3 секунды CPU latency на запрос. Включение по умолчанию экспериментом не обосновано.

## Архитектура

Фактический порядок retrieval:

    BGE-M3 dense top-50 + OpenSearch BM25 top-50
      -> RRF (k=60, candidate cap=24)
      -> PostgreSQL active=true/status=ready
      -> optional CrossEncoder over child search chunks
      -> distinct parents, top-8
      -> unchanged temporal evidence-gate
      -> unchanged Qwen answer/citation repair

Связь child → parent → document сохраняется. Для каждого активного кандидата trace
содержит исходный RRF score, CrossEncoder score, rank before/after и model telemetry.
Ошибка загрузки или inference даёт fallback к исходному RRF order с причиной в trace.
SQL-filter выполняется до reranker и повторно защищён в candidate builder, поэтому
неактивный или не-ready документ не может вернуться через reranking.

Параметры: RERANK_ENABLED=false; RERANK_MODEL=BAAI/bge-reranker-v2-m3;
RERANK_DEVICE=cpu; RERANK_CANDIDATES=24; RERANK_TOP_K=8. Revision задаётся отдельно.
Embeddings, BM25, temporal evidence-gate и citation repair не менялись.

## Corporate v2

- 26 синтетических документов отдельно от неизменённого examples/;
- 36 вопросов: single/multi-document, похожие заголовки, области приказов, даты
  издания и вступления, missing information и distractors;
- устойчивые source_id и fragment_id;
- точные gold quotes валидируются runner до запуска;
- вручную назначенные expected sources/fragments и graded relevance;
- dataset SHA-256:
  6f77bfe0385f4bde4f4e2d17679d61e55b7001335aef493f6eb6d3a27693bf7b;
- corpus SHA-256:
  d9603fc4ffb7ba502485889cea44eaaef53717d6429aafd9a4becf81839ca907;
- config SHA-256:
  6748c04d3dc2183872f245c2deaa52ec5093fd51c4abcd26ff27adad4d5a1891.

Изолированные ресурсы: PostgreSQL schema clip_eval_corporate_v2, Qdrant collection
clip_agent_eval_corporate_v2_dense, OpenSearch index
clip_agent_eval_corporate_v2_sparse, storage eval/runtime/corporate_v2/storage.
Основные ресурсы и прежние проекты не использовались.

## Прогоны

До добавления reranker-кода сохранён baseline
corporate_v2-20261123T164559+0300-f5b2408d. После добавления полной telemetry
проведена контрольная пара, отличающаяся process-only RERANK_ENABLED:

- A, disabled: corporate_v2-20261123T170710+0300-f5b2408d;
- B, enabled: corporate_v2-20261123T170909+0300-f5b2408d.

Comparator подтвердил identical dataset и corpus. Оба прогона: 36/36 completed,
0 errors. В A все 36 trajectories имеют status disabled; в B все 36 — ok,
fallback отсутствовал. Полные локальные run.json, cases.jsonl, report.md и comparison
сохранены в eval/results/; каталог исключён из Git.

Run artifacts записали базовый Git SHA f5b2408 и выполнялись в незакоммиченном
CLIP-005 worktree; точное состояние корпуса и dataset закреплено приведёнными SHA.
Финальный task commit указан в итоговом сообщении и содержит реализацию и отчёт.

Общие настройки: Qwen qwen3.5:9b, think=false, temperature=0, num_predict=512;
BGE-M3 CPU; dense/sparse top-50; RRF k=60; candidates=24; top-8.

## A/B результаты

| Метрика | A: RRF | B: reranker | Delta |
|---|---:|---:|---:|
| Recall@1 | 0.8287 | 0.8843 | +0.0556 |
| Recall@3 | 0.9769 | 1.0000 | +0.0231 |
| Recall@5 | 1.0000 | 1.0000 | 0 |
| Recall@8 | 1.0000 | 1.0000 | 0 |
| MRR | 0.9537 | 1.0000 | +0.0463 |
| nDCG@5 | 0.9682 | 1.0000 | +0.0318 |
| All required sources @8 | 1.0000 | 1.0000 | 0 |
| Gold fragment coverage | 1.0000 | 1.0000 | 0 |
| Required facts pass | 0.8621 | 0.8276 | −0.0345 |
| Forbidden claims clear | 0.8611 | 0.9444 | +0.0833 |
| Citation IDs valid | 1.0000 | 1.0000 | 0 |
| Automatic abstention accuracy | 0.4286 | 0.2857 | −0.1429 |
| Latency mean, ms | 2783.8 | 6082.7 | +3298.9 |
| Latency median, ms | 2521.0 | 5866.5 | +3345.5 |
| Latency p95, ms | 4924.0 | 7621.0 | +2697.0 |
| Input tokens total | 42485 | 41987 | −498 |
| Output tokens total | 4524 | 4750 | +226 |

Rerank latency в B: mean 3276.6 ms, median 3221.9 ms, min 3018.8 ms,
max 5407.7 ms, total 117957.1 ms для 36 вопросов. Дополнительная end-to-end mean
latency практически совпадает с measured rerank mean.

## Анализ качества и регрессий

Retrieval улучшился только в верхней части выдачи: Recall@5 и @8 и multi-source
coverage уже были насыщены в baseline. Reranker устранил graded ranking errors до
MRR и nDCG, равных 1.0, но top-8 evidence set остался полным в обоих режимах.
Изменившийся порядок или состав последних context slots повлиял на Qwen:

- в B появились новые deterministic fact failures CV2-001 и CV2-012;
- CV2-024, наоборот, прошёл fact/citation condition после reranking;
- CV2-023 стал дополнительной automatic abstention failure;
- forbidden-clear улучшился на три кейса;
- допустимость номеров цитат осталась 100%, но это не доказывает entailment.

Семь abstention cases дополнительно просмотрены по исходным документам:
CV2-008, 013, 022, 023, 026, 029 и 034. Review выполнен Codex 2026-11-23 и не
является экспертным юридическим заключением. В обоих режимах корректны 5/7 (0.7143):
CV2-008, 013, 026, 029 и 034. CV2-022 ошибочно переносит 12-месячный срок лицензии на
техническую поддержку; CV2-023 превращает отсутствие условия автопродления в
категорическое «не продлевается».

Разница automatic abstention metric объясняется узким lexical detector: он не всегда
распознаёт корректные формулировки «не устанавливает», «не указано» и «не установлен»
и иногда принимает категорический ответ за отказ. Gold и regex после результатов не
изменялись.

## Модель и ресурсы

- reranker revision: 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e;
- sentence-transformers 4.1.0;
- cache size 2 293 259 337 bytes, около 2,14 GiB;
- CPU: AMD Ryzen 7 9700X, 8 cores / 16 logical processors;
- system RAM: 68 305 268 736 bytes, около 63,6 GiB;
- reranker device: CPU; GPU для reranker не использовался;
- peak/process RAM, CPU utilization, energy and throughput: not measured.

## Ограничения и вывод

Корпус синтетический и небольшой; Recall@5 и @8 насыщен. Automatic facts/citations —
лексические проверки, а не entailment judge. Семантический review всех 36 ответов
человеком-экспертом не выполнялся. CPU cost измерен последовательно на одной машине;
параллельная нагрузка не тестировалась.

Главный критерий CLIP-005 выполнен измерением: CrossEncoder заметно улучшает порядок
первых документов, но при неизменном top-8 не улучшает итоговые ответы и ухудшает
latency. Поэтому reranker остаётся выключенным по умолчанию. Дальнейшее исследование
меньшего candidate cap или context budget должно быть отдельным экспериментом.
