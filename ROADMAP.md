# Evolution roadmap · ordered for a short hackathon

## P0 — before accepting organizer data

- Verify Python 3.12 environment, Docker ports and Ollama GPU access; prefetch all models.
- New Postgres DB, Qdrant collection and OpenSearch index; test a fresh upload and a simple question.
- Demo UI / trace logging / 3-minute script; synthetic reproducible examples.
- Profile model memory and latency. If CPU rerank is too slow, disable temporarily with flag, record quality tradeoff.

## P1 — after 26 Nov detailed spec

- Confirm format distribution, whether DOCX tables and scanned PDFs matter, document metadata and actual evaluation method.
- Add OCR/table parsing **only if corpus needs it**; do not spend time on hypothetical complexities.
- Add source-side clause/section IDs only if actual documents contain dependable structure.
- Create 15–25 hand-checked evaluation questions and latency logs. Use `docs/EVALUATION.md`.
- Improve query decomposition / multi-document coverage; assess omissions and contradictions.
- Rehearse an uncut 3-minute recording with warm models and real corpus. Freeze SHA for submission.

## P2 — stretch features within hackathon

- Evidence coverage per subquestion and rule-specific conflict detection.
- Source-backed shared verified memory with approval/expiry and source checks.
- Retrieval ACL demo (synthetic groups) *if other high-priority criteria already pass*.
- One dashboard view of missing product/license information and frequently unsuccessful queries.
- Export experiment reports by git commit, corpus snapshot and model config.

## P3 — after hackathon / before deployment

- Actual SSO, RBAC/ABAC, document ACL on **every** retrieval stage (not UI-only), encryption, logging privacy, retention.
- Real parser/OCR workers and virus scanning; backpressure, job queue, idempotent uploads and index reconciliation/outbox.
- Alembic schema migrations, index rebuild CLI, snapshotting and backup/restore drills.
- Rule-level semantics: issuer hierarchy and explicit amendment/supersession graph; provenance confidence.
- Full evaluation suite with human review, trustworthy citation support checking and out-of-distribution tests.
- Production observability: OpenTelemetry tracing, Prometheus, optional Langfuse/MLflow (self-hosted only).
- Optional conditional ReAct tools for authorized corporate integrations, with typed permissions, approval and hard resource limits.
- Experimental 10B Giga embeddings behind adapter, compare Russian recall/latency/VRAM with BGE-M3; no automatic migration.

**Decision gate:** do not add a feature unless it improves one of the scored objectives, resolves a verified corpus limitation or reduces operational risk.
