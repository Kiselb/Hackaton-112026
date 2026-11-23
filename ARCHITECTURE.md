# Architecture · v0.1

## Goals

1. Find evidence across multiple corporate regulations, independent orders, product descriptions and licensing rules.
2. Always reveal the document, page (when actually available), paragraph and search span inside the parent context.
3. Work with fully local inference and no cloud data processing at runtime.
4. Record agent trajectories and produce searchable quality analytics even without customer-provided golden answers.
5. Make it easy to replace search/index/model components through narrow adapters.

## System boundary

- **Presentation:** React+TS, two views (search/admin), demo identities.
- **Application/API:** FastAPI request validation, upload ingestion, query lifecycle, feedback.
- **Agent:** LangGraph, bounded graph, evidence-based answer synthesis.
- **Model adapters:** Ollama Qwen3.5-9B, SentenceTransformers BGE-M3 / BGE reranker.
- **Stores:** Postgres relational source of truth; Qdrant and OpenSearch are rebuildable derived indexes; file storage is filesystem adapter.
- **Observability:** structured node decisions, retrieval details, token counters, response latency, outcomes; DB for MVP, OTel/MLflow/Langfuse later.

## Ingestion

1. Validate extension and size → immutable source bytes under UUID storage_key.
2. Extract paragraphs from PDF (page number), DOCX/TXT/MD (no reliable physical page).
3. Group adjacent paragraphs per page into ~1700-character **parent** fragments; carve out exact ~550-character overlapping **search** spans with stored offsets.
4. Insert normalized metadata, revision, parent and child rows into a **new Postgres DB**.
5. Run local dense embeddings; index child vectors in **new Qdrant collection** and child text in **new OpenSearch index**.
6. Mark revision ready/active. A new revision of the **same document lineage** deactivates previous active revisions; independent dated orders remain separate document lineages.
7. Soft-deactivation hides evidence in indexes and Postgres; source bytes and DB metadata remain for recovery.

Important: a revised regulation is *technical lineage*; replacing a particular business rule across **different orders** requires explicit semantic analysis and cannot be assumed from the latest upload date.

## Search / answer

```text
question + user session history
    -> LLM query plan (original query always preserved)
    -> local BGE-M3 embedding
    -> Qdrant dense top-50 + OpenSearch BM25 top-50 per query
    -> RRF, candidate cap 24
    -> local CrossEncoder rerank
    -> SQL visibility: status=ready, active=true, effective as-of date
    -> distinct parents, up to 8, approximate 5000-token budget
    -> LLM evidence-coverage judge
         -> (insufficient) one targeted query extension, then retrieve again
    -> LLM sourced synthesis [1]...[N]
    -> syntactic citation-ID validation / abstention
    -> trace + messages, feedback possible
```

**Evidence is always SQL-filtered** even if stale derived search indexes return older records. Numeric citation ID maps to an immutable search child, whose parent text is displayed with its exact start/end offsets. The generator sees the parent context; the UI highlights the search child.

Multi-document questions: planner decomposes into separate search forms. In a future iteration, store subquestion-to-document coverage and require one grounded source per subquestion. Current judge may be imperfect.

## Document temporality and contradiction

- `Document` = identity/lineage, e.g. “Регламент договоров”.
- `Revision` = immutable uploaded edition; metadata includes `effective_from`, `effective_to`, and `active` flag.
- **Separate orders** each have independent identities; two active orders might regulate one business fact.
- Date filtering considers an explicitly specified `as_of` date (defaults to current calendar date). Missing dates are **unknown**, not proof of effect.
- Evidence from conflicting orders is shown together; answer should say that precedence cannot be proven instead of picking the newest blindly.
- A future **norm graph** would model `rule_id`, clause-level `supersedes`, effective intervals and issuer authority, plus human verification. Not in MVP.

## Memory taxonomy

See [MEMORY.md](MEMORY.md). Neither arbitrary user requests nor unverified model answers become org-wide knowledge. Trace history is feedback material rather than an additional source of business truth.

## Sequence & failure boundaries

Ingestion is not a distributed transaction across PG, Qdrant, OpenSearch and filesystem. The current MVP marks failed revisions in PG, and retrieved hits are filtered by PG readiness/active state. **Known weakness:** a failure between index updates and database commit can desynchronize visibility; implement reconciliation or an outbox worker before deployment. Search service outages return HTTP 503, never a fabricated answer. Operational readiness depends on all three stores and Ollama; `/healthz` is a process health indicator only, not a full readiness probe.

## Scale/performance

For a small corpus and 100–300 queries/month, indexes and model inference can fit on one workstation. Run embedding/reranker on CPU while Ollama uses GPU, profile first; later use GPU batch windows. Search runs sequentially per query variant to limit peak memory use. For a multiuser organization, plan API workers, queue, GPU concurrency limits, access-controlled document filters, backup and index rebuild/reconciliation.

## Security caveats

No real ACL, authentication, admin protection or user identity verification in MVP; user_id may be spoofed. Data must be synthetic/approved and bound to loopback only. Source text is untrusted data; never allow a retrieved document to change tool permissions. Enforce upload validation, antivirus scans, PDF parsing sandbox, secrets management and role-aware retrieval in production.
