# Codex work queue (issue/PR-sized, in execution order)

Each task needs changed code, targeted tests, docs update and explicit definition of done. Require **Python 3.12** and no external LLM APIs.

## CLIP-001 — smoke test and target-environment reproducibility (P0)

Validate `README.md` in WSL Python 3.12; check Ollama GPU serving, Postgres DB name, Qdrant collection, OpenSearch index. Confirm startup and UI. Add end-to-end script performing upload→ask→deactivate→ask→restore→ask. Save timings and observed VRAM/RAM in `docs/BASELINE_RESULTS.md` (do not invent numbers).

**DoD:** green full smoke on target workstation, no writes to former `rag` DB or previous indexes, local logs demonstrate 0 external API requests.

## CLIP-002 — safe indexing, resilience and consistency (P0/P1)

Refactor upload into a state machine (`pending/indexing/ready/failed`) with idempotency key and index reconciliation. Fix partial failures across Postgres/Qdrant/OpenSearch, ensure new revisions cannot leave old active revision invisible if update fails. Test failure injection at each boundary. Add repair CLI.

**DoD:** simulate timeouts and restart; only `ready && active` revisions appear; recover via reindex, no silent data loss.

## CLIP-003 — corpus extraction fidelity (P1)

On actual 26 Nov corpus, characterize formats (PDF text, scans, DOCX tables, footers). Improve paragraph anchors and page metadata, introduce OCR/table adapter only when evidence requires it. Document ambiguous page numbers honestly.

**DoD:** sample audit of 10 documents with exact source spans, extraction-error report and reproducible tests.

## CLIP-004 — evaluation harness and judged evidence (P1)

Add JSONL runner using `examples/eval_seed.jsonl` enriched by expert labels. Compute Recall@5/10, coverage per subquestion, citation **entailment manual labels**, abstention correctness, p50/p95 and token count. Generate report keyed by git SHA, corpus hash and model IDs. Support dense-only, BM25-only and full ablations.

**DoD:** reproducible baseline table, no model-self-scoring as only judge.

## CLIP-005 — improve agent plan / conflict reasoning (P1)

Add explicit subquestions into LangGraph state, require evidence per subquestion and detect contradictory parameters/dates. Re-query only missing components. Continue preserving bounded retries and source IDs. Flag uncertain precedence, not merely recency sorting.

**DoD:** passes at least 3 hand-checked multi-document and 3 contradiction cases, no extra hallucinated citation IDs.

## CLIP-006 — safe, approved shared memory (P2)

Add approved memory retrieval by scope and source-revision validity; memory records should only navigate toward original documents. Add admin approval UI, archival invalidation, fake-user isolation tests, optional retention. Never share raw conversation transcripts by default.

**DoD:** no unapproved cross-user fact appears in answers; referenced revision is active and accessible.

## CLIP-007 — security hardening and deployment (P3)

Add SSO/auth, role permissions, per-document ACL in both indexes and SQL final filter, virus scan, upload limits, privacy/redaction, encryption and audit, Alembic, backup/restore and metrics export. Deployment playbook.

**DoD:** authenticated role-restricted integration tests, repeatable migrations/rollback and security review.
