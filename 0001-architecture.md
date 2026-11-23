# ADR-0001 · Bounded agentic RAG instead of open-ended ReAct

Status: **Accepted and implemented in CLIP-006** · 2026-11-23

Context: User tasks are evidence retrieval across possibly multiple documents. Judges separately score agent behaviour and search analytics. Time, VRAM and reliability are limited.

Decision: Implement explicit LangGraph graph `route_and_plan → retrieve → assess_evidence
→ [targeted_retrieve → assess_evidence] → compose_answer → validate_answer → finish`,
with local inference. Conditional edges and configured call/retry budgets are mandatory.
No generic uncontrolled tool-using ReAct loop in MVP. Node decisions and metrics are
durable traces.

Consequences: Deterministic upper bound on search loops, inspectable trajectories and
extension points for later tools. The agent remains experimental and disabled by default:
corporate_v2 improved abstention/forbidden-claim metrics but increased latency and did
not improve required-facts detection. It cannot autonomously perform arbitrary
backoffice actions, and trace state is not long-term organizational memory.
