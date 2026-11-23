# Memory design

Four useful, non-overlapping layers (labels are implementation-oriented; different literature uses different taxonomies):

| Layer | Mechanism in MVP | Trust / future action |
|---|---|---|
| Working / short-term | Postgres `conversation_messages`, keyed by `(user_id, session_id)`, last 8 turns passed to planner/answer | Demo user identity only; no cross-user mixing |
| Semantic / organizational | Indexed **source** regulations/products and licensing docs + Postgres parent/child chunks | Document approval and temporal status; not agent opinions |
| Episodic | `search_traces` node events, retrieved IDs, usage and feedback | Used for offline failure analysis / dataset curation, **not automatically injected** into future answers |
| Procedural | Versioned system instructions, graph logic, retrieval configuration, tests and ADRs in Git | Improvements require reviewed releases and measured eval |

## Controlled shared memory

An organization may have **curated institutional knowledge**, but copying all chats into a shared vector store is risky: incorrect answers become self-reinforcing, private information leaks and conflicting old facts remain alive.

MVP includes an **admin-only by convention** verified-memory table / API. Each record references an **active, indexed revision** and an approving person. This API is open in demo mode; production must protect it. The records are intentionally **not included in inference yet**. The next safe step is to retrieve these as *navigation hints* and verify each against live source evidence before using it. Mark stale or inaccessible references as invalid. Add approval, scope, expiration, provenance and ACL before real rollout.

## Continuous learning without dangerous online fine-tuning

1. Collect traces and thumbs-up/down (with privacy safeguards).
2. Cluster frequent unanswered topics, missing documents, outdated source references and retrieval errors.
3. Experts turn failures into labeled question → expected evidence → acceptable answer / abstention examples.
4. Compare candidate prompts, reranker and retrieval versions offline against frozen snapshots; only promote improvements that beat baseline.
5. Consider distillation/fine-tuning only when enough approved examples exist. Do not tune on unverified LLM outputs.

Memory isn't simply “agent remembers everything”. It is a provenance-aware knowledge lifecycle with separate trust levels and retention policies.
