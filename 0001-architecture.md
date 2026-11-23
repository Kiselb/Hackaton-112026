# ADR-0001 · Bounded agentic RAG instead of open-ended ReAct

Status: **Accepted** · 2026-11-23

Context: User tasks are evidence retrieval across possibly multiple documents. Judges separately score agent behaviour and search analytics. Time, VRAM and reliability are limited.

Decision: Implement explicit LangGraph graph `plan → retrieve → assess → [retry once] → answer`, with local inference. No generic uncontrolled tool-using ReAct loop in MVP. Node decisions and metrics are durable traces.

Consequences: Deterministic upper bound on search loops, inspectable trajectories, extension points for later tools. Cannot autonomously perform arbitrary backoffice actions yet, and LLM calls have non-trivial latency.
