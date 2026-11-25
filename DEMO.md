# Uncut demo script (target <= 3:00)

**Pre-flight** (before recording; not part of take): start DB / Qdrant / OpenSearch /
Ollama and either the local backend/frontend or the authenticated CLIP-010 stand. For a
public take, complete backup and the local/external auth checks from
`docs/CLIP010_TAILSCALE_DEMO.md`; never expose Vite or FastAPI directly. Download weights,
warm CPU reranker and GPU model, load approved corpus, disable browser notifications,
zoom UI to readable size, close sensitive tabs. Keep a prepared second tab on admin and a
fixed example question. One continuous capture; no editing.

| Time | Action | Key evidence |
|---|---|---|
| 0:00–0:15 | Show one-line task and local dashboard | Local model + current document catalog |
| 0:15–0:45 | Ask question involving two different regulations/orders | Shows multi-query planning and retrieved evidence |
| 0:45–1:20 | Show answer + numbered citations + highlighted search chunks inside parent | Users can verify evidence and location |
| 1:20–1:50 | Ask deliberately conflicting-dates or missing-doc question | Agent acknowledges uncertainty; no invented fact |
| 1:50–2:15 | Admin: show revisions, deactivate older regulation or upload a short new one | Freshness lifecycle, reindexing (only if speed reliable) |
| 2:15–2:40 | Show search trajectory with retry/coverage decision and token usage | Agentic multi-step behavior, observability |
| 2:40–2:55 | Show analytics on failed requests, popular questions and feedback | Partner's analytics objective |
| 2:55–3:00 | State offline deployment and repo SHA | Reproducible deliverable |

Record a timed rehearsal first: local CPU reranking can exceed this budget. If it does, show the pre-indexed revisions only, choose one multi-document question and allocate more time to the answer; keep traces visible. Do not fake speed or edit the video.

Submission checklist: ≤3-minute single uncut video; one-page product description in official template; git URL with commit history; exact final commit hash (`git rev-parse HEAD`); reuse/dependency disclosure and cost estimate.
