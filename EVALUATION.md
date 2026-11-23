# Experimental protocol / quality laboratory

## Dataset reality

Organizer responses disagree: Q1 says a reference test dataset will be supplied, while Q2/Q4 say **no** golden corpus or reference answers; Q5 again mentions a test dataset. Until the detailed task brief on 2026-11-26, assume **no trusted gold**. Never fabricate quality statistics.

## Create small annotated evaluation set (start 15–25 questions)

- Single-document factual retrieval and clause finding (5).
- Multi-document procedural question: who/what/sequence (5).
- Conflicting dated orders where precedence is ambiguous (3–5).
- Licensing rules involving multiple product/plan conditions (3–5).
- Missing documents / impossible questions to test abstention (3–5).
- Variants with paraphrases and multi-turn follow-ups.

Each case records `question`, `as_of`, `expected_revision_ids`, `required_claims`, `should_abstain`, `risk`, and optionally `acceptable_answer`. Human labeling is the actual ground truth. Include synthetic cases now; replace/add organizer cases only when allowed.

## Metrics

- **Retrieval:** Recall@5/10, MRR@10, multi-document evidence coverage; measure dense-only / BM25-only / RRF / reranker.
- **Generation:** grounded answer accuracy and citation **support** rate (human reviewed), citation ID validity, correct refusal for conflicts/missing data.
- **Operational:** latency p50/p95, token inputs/outputs, re-query rate, failures, index lag, peak GPU VRAM/RAM, throughput.
- **Business:** no-answer share, frequent questions with no documents, search abandonment, feedback trends.

The syntactic validator in MVP only rejects nonexistent `[N]` references; it does **not** guarantee that a statement is entailed by the cited passage. LLM-as-judge should be a **secondary diagnostic**, calibrated against expert labels; do not optimize solely to the same judge model that generates answers.

## Experimental stages

1. Baseline single query → hybrid → source synthesis.
2. Enable `MULTIQUERY_ENABLED`, compare retrieval coverage and time.
3. Enable `RERANK_ENABLED`, compare Recall and attribution accuracy, record CPU cost.
4. Enable `AGENT_JUDGE_ENABLED`, measure correctly recovered missing evidence and wasted retries.
5. Tune parent/child sizes and evidence budget.
6. Add explicit temporal-conflict fixtures and verify uncertainty.

Every experiment must record corpus snapshot ID, model name/revision/quantization, prompt version, index names, git commit SHA, hardware and temperature. Compare on exactly the same snapshot.

## First local smoke checklist

- Models are downloaded before disconnected runs.
- Fresh Postgres DB / Qdrant collection / OpenSearch index contain only the demo project.
- Admin upload creates ready/active revision.
- `ask` returns only locally evidenced sources, with parent and highlighted child.
- Contradictory dates don't silently collapse to one answer.
- Missing-answer request abstains without fake citation.
- Deactivated revision is excluded from candidate evidence; restoration works.
- A different demo session does not inherit prior chat turns.
- Analytics and trace timeline update after queries.
- End-to-end timing and peak VRAM are recorded on the actual workstation.
