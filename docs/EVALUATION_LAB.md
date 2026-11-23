# Evaluation Lab

## Назначение

Контур фиксирует baseline и контролируемые эксперименты single-query hybrid RAG.
MultiQuery, LangGraph и LLM Judge отсутствуют; reranking доступен за флагом. Набор
eval/datasets/corporate_v1.jsonl содержит 18 сценариев,
вручную сверенных с синтетическими файлами examples/. Идентификаторы источников имеют
вид example:order_101 и не зависят от UUID, создаваемых ingestion.

Результаты на этом маленьком синтетическом корпусе показывают только
воспроизводимость и обнаружимые регрессии. Они не прогнозируют качество на документах
организатора или на производственном корпусе.

## Запуск

Требуются Python 3.12, доступные PostgreSQL, Qdrant, OpenSearch и Ollama с моделью из
.env. Backend отдельно запускать не нужно:

    .\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval/datasets/corporate_v1.jsonl

Runner проверяет /readyz и наличие модели Ollama, индексирует четыре документа при
первом запуске, затем последовательно вызывает тот же /api/v1/ask, который является
baseline приложения.

Для CLIP-005 добавлен независимый corporate_v2: 26 документов в
eval/corpora/corporate_v2/documents, явный corpus.json и 36 вопросов. В manifest
заданы устойчивые source/fragment ID и точные цитаты, наличие которых runner проверяет
до обращения к сервисам. corporate_v1 и examples/ не изменены.

## Изоляция и идемпотентность

Для corporate_v1 используются отдельные ресурсы:

| Ресурс | Изолированное имя |
|---|---|
| PostgreSQL schema | clip_eval_corporate_v1 |
| Qdrant collection | clip_agent_eval_corporate_v1_dense |
| OpenSearch index | clip_agent_eval_corporate_v1_sparse |
| Files | eval/runtime/corporate_v1/storage |

Основные таблицы, коллекция clip_agent_dense_v1, индекс clip_agent_sparse_v1 и
storage/ не затрагиваются. SHA-256 manifest связывает версию корпуса с точными байтами
examples/*.txt. При полном совпадении повторная индексация пропускается. Частичное
состояние, отсутствующий manifest или изменение документов останавливают прогон;
runner ничего не удаляет. Изменённый корпус должен получить новую версию датасета.

corporate_v2 аналогично использует schema clip_eval_corporate_v2, collection
clip_agent_eval_corporate_v2_dense, index clip_agent_eval_corporate_v2_sparse и
eval/runtime/corporate_v2/storage. Corpus config выбирается явно через
--corpus-config либо по default eval/corpora/<dataset stem>/corpus.json.

## Схема сценария

Каждая JSONL-строка содержит обязательные case_id, question, category,
expected_sources, required_facts, forbidden_claims, should_abstain и notes. Условие
обязательного факта задаёт проверяемый regex, режим объединения, ожидаемый устойчивый
source ID и необходимость цитаты. Запрещённое утверждение имеет ID, описание и regex.

Эти правила создавались человеком по исходным текстам. Они не являются ответами,
сгенерированными оцениваемой моделью.

## Метрики

- Document Recall@1, @3, @5, @8 и MRR считаются по устойчивым ID документов.
- nDCG@5 считается только для сценариев с вручную заданными relevance grades.
- Expected document coverage показывает долю ожидаемых документов среди источников.
- All-required-sources@8 и fragment coverage отдельно проверяют multi-document полноту.
- Номера [N] извлекаются и проверяются на допустимый диапазон.
- Однозначные факты проверяются узкими регулярными условиями; если нужна цитата,
  предложение должно ссылаться на ожидаемый документ.
- Запрещённые формулировки и детерминированные маркеры отказа проверяются отдельно.
- Сохраняются mean, median, nearest-rank p95 latency, input/output tokens.
- Для reranker отдельно сохраняются latency, status/fallback reason, модель/revision,
  RRF/CrossEncoder scores и ранги каждого активного кандидата.

Регулярное условие и корректный номер ссылки **не доказывают entailment**. Сложные
multi-document, конфликтные, недостаточные и abstention-сценарии получают
manual_review_required=true. LLM Judge намеренно отсутствует.

## Артефакты

Каждый каталог eval/results/<experiment_id>/ содержит:

- run.json — версия формата, время, commit SHA, dataset/corpus hash, модели,
  generation/retrieval settings, readiness и агрегаты;
- cases.jsonl — ответ, ранжированные document/chunk UUID, устойчивые source ID,
  полная candidate-траектория, проверки, токены, время, ошибка и признак ручной
  проверки каждого сценария;
- report.md — читаемый агрегированный отчёт.

Runtime и результаты исключены из Git. Формат clip-evaluation-run/v1 сохраняет
параметры retrieval независимо от будущего названия стратегии, поэтому MultiQuery и
reranking смогут писать те же поля и сравниваться с baseline.

## CLIP-005 A/B

    $env:RERANK_ENABLED='false'
    $env:RERANK_MODEL_REVISION='953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'
    .\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval/datasets/corporate_v2.jsonl

    $env:RERANK_ENABLED='true'
    .\.venv\Scripts\python.exe scripts\evaluate.py --dataset eval/datasets/corporate_v2.jsonl

Меняется только RERANK_ENABLED; model ID/revision, corpus/dataset SHA, retrieval и
generation settings должны совпадать. Фактический вывод и ограничения находятся в
docs/reports/CLIP005_RERANKING.md.

## Сравнение

    .\.venv\Scripts\python.exe scripts\evaluate.py --compare RUN_A RUN_B

Опциональный --comparison-output сохраняет Markdown и JSON. Команда возвращает код 2,
если dataset metadata или corpus hash различаются: такое сравнение не считается
валидным экспериментом на одинаковом корпусе.
