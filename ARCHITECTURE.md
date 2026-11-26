# Architecture · v0.1

## Goals

1. Find evidence across multiple corporate regulations, independent orders, product descriptions and licensing rules.
2. Always reveal the document, page (when actually available), paragraph and search span inside the parent context.
3. Keep local Ollama as the default; allow an explicitly configured request-level OpenAI
   path without hidden fallback or external retrieval tools.
4. Record agent trajectories and produce searchable quality analytics even without customer-provided golden answers.
5. Make it easy to replace search/index/model components through narrow adapters.

## System boundary

- **Presentation:** React+TS, диалоговый поиск и admin views; уникальная identity и
  ownership token живут в `sessionStorage` конкретной вкладки.
- **Application/API:** FastAPI request validation, upload ingestion, query lifecycle, feedback.
- **Agent:** LangGraph, bounded graph, evidence-based answer synthesis.
- **Model adapters:** request-scoped Ollama/OpenAI LLM session, SentenceTransformers
  BGE-M3 и опциональный BGE CrossEncoder reranker.
- **Stores:** Postgres relational source of truth; Qdrant and OpenSearch are rebuildable derived indexes; file storage is filesystem adapter.
- **Observability:** structured node decisions, retrieval details, token counters, response latency, outcomes; DB for MVP, OTel/MLflow/Langfuse later.
- **Analytics UI:** deterministic overlapping outcome labels over `SearchTrace`, bounded
  list/detail APIs and trace-linked feedback. No LLM judge is required.
- **Quality service:** trace-bound, user-confirmed escalation; backend-only SMTP;
  separately authorized expert draft/approval; immutable knowledge versions published as
  ordinary Documents only after dense+sparse indexing succeeds.
- **Optional public demo ingress:** Tailscale Funnel HTTPS termination → loopback-only Caddy with
  bcrypt Basic Auth → production React files and loopback-only FastAPI. It reuses the
  same application configuration and stores; it is not another deployment.

## Ingestion

1. Validate extension and size → immutable source bytes under UUID storage_key.
2. Extract paragraphs from PDF (page number), DOCX/TXT/MD (no reliable physical page).
3. Group adjacent paragraphs per page into ~1700-character **parent** fragments; carve out exact ~550-character overlapping **search** spans with stored offsets.
4. Insert normalized metadata, revision, parent and child rows into a **new Postgres DB**.
5. Run local dense embeddings; index child vectors in **new Qdrant collection** and child text in **new OpenSearch index**.
6. Mark revision ready/active. A new revision of the **same document lineage** deactivates previous active revisions; independent dated orders remain separate document lineages.
7. Soft-deactivation hides evidence in indexes and Postgres; source bytes and DB metadata remain for recovery.

Important: a revised regulation is *technical lineage*; replacing a particular business rule across **different orders** requires explicit semantic analysis and cannot be assumed from the latest upload date.

## Quality-service knowledge ingestion

`quality_review_required` and `conflict_detected` may create one `QualityRequest` only
after the conversation ownership token and idempotency key are checked. Drafts are not
searchable. A separate demo approval capability snapshots expert-authored text into an
immutable `QualityKnowledgeEntry`. Publication creates the existing `Document`/`Chunk`
objects with `active=false,status=indexing`, runs the same Qdrant/OpenSearch pipeline and
sets `active=true,status=ready` only after both derived indexes complete. Thus partial
indexing cannot bypass PostgreSQL visibility. Retrieval decorates source provenance but
keeps ordinary citation, scope, temporal, conflict and grounding checks.

## Search / answer

```text
question + user_id + session_id
    -> resolve allowlisted request-level LLM provider/model (one session, no fallback)
    -> deterministic input guard (language, meaningfulness, scope, injection)
    -> AGENT_ENABLED=false: strict single-query RAG
    -> AGENT_ENABLED=true (default for new configs): LangGraph
         route_and_plan (simple fast route or bounded JSON plan)
         -> retrieve (existing BGE-M3 + BM25 + RRF + SQL visibility + optional rerank)
         -> assess_evidence per aspect
              -> (gap and budget available) targeted_retrieve -> assess_evidence
              -> compose_answer
         -> validate_answer (citations, scope/entailment guards, bounded repair, fail-closed)
    -> outcome (answered / clarification / quality review / conflict / rejection / technical)
         -> finish
    -> PostgreSQL SearchTrace with provider/model, node trajectory, decisions, queries,
       source IDs, gaps, per-node latency, actual tokens and bounded call counters
```

## Stateful clarification dialogue

`Conversation` хранит исходный вопрос, статус, хеш непредсказуемого ownership token,
ограниченный список пользовательских уточнений, счётчики, version и expiry.
`ConversationTurn` хранит упорядоченные user/assistant turns и ссылку на отдельный
`SearchTrace`. Таблицы additive: существующие `search_traces` не переписываются.

```text
new agent question -> processing -> answered -> completed
                               \-> clarification_required -> awaiting_clarification
                                      -> guarded short answer -> processing
                                      -> fresh LangGraph + fresh retrieval + strict validation
                                      -> awaiting_clarification | completed
awaiting_clarification -> close -> closed
awaiting_clarification -> TTL -> expired
```

Продолжение требует `conversation_id` и заголовок `X-Conversation-Token`; в БД хранится
только SHA-256-хеш, сравнение выполняется constant-time. `SELECT ... FOR UPDATE` и
промежуточный статус `processing` не позволяют двум follow-up раздвоить состояние.
Контекст формируется как bounded `resolved_query`: исходный вопрос остаётся отдельным,
уточнения помечены как пользовательские условия, а не корпоративные факты. Каждый ход
заново проходит SQL visibility gate, поэтому прежние source IDs и деактивированный между
ходами документ не подтверждают итоговый ответ.

Baseline RAG не продолжает ожидающий agent dialogue. Provider/model фиксируются новым
`LlmSession` только на текущий ход; между ходами пользователь может явно изменить модель,
не изменяя прошлые turns/traces. Максимумы по умолчанию: два вопроса, три ответа, 1000
символов на сообщение, 2000 символов контекста и TTL 60 минут.

**Evidence is always SQL-filtered** even if stale derived search indexes return older records. Numeric citation ID maps to an immutable search child, whose parent text is displayed with its exact start/end offsets. The generator sees the parent context; the UI highlights the search child.

Reranking is disabled by default and runs only after the PostgreSQL visibility gate.
It scores child search chunks while preserving their parent/document linkage. Trace
stores RRF score, CrossEncoder score and ranks before/after. A model load or inference
error falls back to the original RRF order and records the reason; it cannot reintroduce
a document removed by the visibility gate. Temporal evidence-gate and citation repair
remain downstream and unchanged.

Multi-document questions: planner decomposes into separate aspects and search forms while
the original question remains first. Assessment records aspect-to-source coverage and gaps.
Planner errors preserve the original question; assessor/validator JSON errors fail closed.
The assessment is not an independent source of facts and remains imperfect.

Agent limits are enforced before calls: at most three search calls, one retry and four
LLM calls by default. The only graph loop is
`assess_evidence -> targeted_retrieve -> assess_evidence`; retry/search budgets and
LangGraph recursion limit make it finite. State is request-local and is not long-term or
shared memory.

The same turn-scoped LLM session is used for planning, evidence assessment, answer
composition, citation repair and grounding validation. OpenAI uses the Responses API with
`store=false`, no hosted tools and strict server-side model allowlisting. Missing credentials,
provider failure, refusal, incomplete or empty output fail closed and never switch provider.

## Search analytics and feedback

`SearchTrace.status` is the technical execution status only. CLIP-007 derives separate,
overlapping categories (`technical_error`, `no_evidence`, `evidence_gap`,
`justified_abstention`, `citation_problem`, `agent_fallback`,
`manual_review_required`, `answer_produced`) from persisted counters and structured
details. This classification is deterministic and does not claim semantic correctness;
without a verified signal answer correctness is `not_measured` and a normal answer's
quality status is `unknown`.

`trace_feedback` stores a rating, trace ID and the trace's user/session identity. The API
requires both identities to match before insertion. Feedback is analysis material only and
never becomes document evidence or a corporate fact. The additive table is created by the
existing `Base.metadata.create_all` startup path; existing tables and indexes are not
rewritten.

The list API caps pages at 100 rows and supports status, mode, outcome, identity and time
filters. Detail reconstruction resolves only the recorded `source_ids` through PostgreSQL,
preserves their citation order and returns page only when it is stored. Raw exception text
is retained in PostgreSQL but redacted from the frontend projection.

`AGENT_DEMO_OVERRIDE_ENABLED=false` is the safe default. When explicitly enabled on a
loopback-only local demo or behind the authenticated CLIP-008 ingress, `/api/v1/ask`
accepts a request-level baseline/agent choice; it does not mutate global settings. The
response and trace always expose the actual mode. Agent exceptions return
`technical_error`; baseline is never started implicitly.

## Public demo ingress and overload boundary

`PUBLIC_DEMO_ENABLED=true` enables process-local, non-blocking semaphores around the whole
ask and ingestion request. Capacity comes from `DEMO_MAX_CONCURRENT_LLM` and
`DEMO_MAX_CONCURRENT_INGEST`; excess work receives HTTP 429 with `Retry-After` rather than
an unbounded queue. Context-managed release prevents a failed Ollama/indexing call from
leaking capacity. Upload bytes are bounded independently in Caddy and FastAPI.

Caddy's site-wide route authenticates before any handler, including `/api`, `/healthz`,
`/readyz`, static files and SPA fallback. It strips `Authorization` before proxying and
from access logs. Caddy and FastAPI bind only to loopback. Tailscale Funnel is the only
external endpoint and terminates TLS, so the transport provider remains inside the trust
boundary. Tailscale Serve is private to the tailnet and is not used as a public substitute.

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

No real ACL or user identity verification exists; `user_id` may be spoofed. The optional
CLIP-008 ingress supplies only one shared Basic Auth credential and protects admin routes
at the proxy boundary. Direct FastAPI must remain loopback-only. Data must be approved for
the Tailscale Funnel trust boundary. Source text is untrusted data; never allow a retrieved document
to change tool permissions. Enforce antivirus scans, PDF parsing sandbox, managed secrets,
rate limiting and role-aware retrieval in production.
