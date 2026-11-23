from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

from .db import SearchTrace


FEEDBACK_RATINGS = {"helpful", "incomplete", "incorrect", "missing_documents"}
TECHNICAL_SUCCESS_STATUSES = {"ok"}
JUSTIFIED_REFUSAL_REASONS = {
    "no_evidence",
    "temporal_evidence_gap",
    "support_term_not_defined",
    "auto_renewal_not_defined",
    "answer_validation_failed",
}


def _details(trace: SearchTrace) -> dict[str, Any]:
    return trace.details if isinstance(trace.details, dict) else {}


def trace_mode(trace: SearchTrace) -> str:
    return "agent" if _details(trace).get("mode") == "langgraph_agent" else "baseline"


def _citations_valid(details: dict[str, Any]) -> bool | None:
    validation = details.get("validation")
    if isinstance(validation, dict) and "citations_valid" in validation:
        return bool(validation["citations_valid"])
    if "citations_valid" in details:
        return bool(details["citations_valid"])
    return None


def _refusal_reason(details: dict[str, Any], no_evidence: bool) -> str | None:
    value = details.get("refusal_reason")
    if isinstance(value, str) and value:
        return value
    if details.get("temporal_guard_applied"):
        return "temporal_evidence_gap"
    return "no_evidence" if no_evidence else None


def _has_fallback(details: dict[str, Any]) -> tuple[bool, bool]:
    agent_fallback = bool(details.get("agent_fallback_reason"))
    reranking = details.get("reranking")
    rerank_fallback = isinstance(reranking, dict) and (
        reranking.get("status") == "fallback" or bool(reranking.get("fallback_reason"))
    )
    return agent_fallback or rerank_fallback, agent_fallback


def classify_trace(
    trace: SearchTrace, feedback_ratings: Iterable[str] = ()
) -> dict[str, Any]:
    """Return overlapping deterministic outcome labels without judging correctness."""
    details = _details(trace)
    feedback = set(feedback_ratings)
    technical_error = trace.status not in TECHNICAL_SUCCESS_STATUSES
    no_evidence = not technical_error and (
        trace.retrieved_count == 0 or details.get("refusal_reason") == "no_evidence"
    )
    refusal_reason = _refusal_reason(details, no_evidence)
    gaps = details.get("gaps")
    evidence_gap = bool(gaps) or refusal_reason in JUSTIFIED_REFUSAL_REASONS - {"no_evidence"}
    if "missing_documents" in feedback:
        evidence_gap = True
    citations_valid = _citations_valid(details)
    citation_problem = trace.selected_count > 0 and citations_valid is False
    fallback, agent_fallback = _has_fallback(details)
    justified_abstention = refusal_reason in JUSTIFIED_REFUSAL_REASONS
    manual_review = (
        no_evidence
        or evidence_gap
        or citation_problem
        or agent_fallback
        or bool(feedback & {"incomplete", "incorrect", "missing_documents"})
    )
    answer_produced = bool(trace.answer) and not technical_error and not justified_abstention

    flags = {
        "technical_error": technical_error,
        "no_evidence": no_evidence,
        "evidence_gap": evidence_gap,
        "justified_abstention": justified_abstention,
        "citation_problem": citation_problem,
        "agent_fallback": agent_fallback,
        "manual_review_required": manual_review,
        "answer_produced": answer_produced,
    }
    categories = [name for name, enabled in flags.items() if enabled]
    if technical_error:
        quality_status = "not_measured"
    elif manual_review:
        quality_status = "manual_review_required"
    elif justified_abstention:
        quality_status = "justified_abstention"
    else:
        quality_status = "unknown"
    return {
        "technical_status": "error" if technical_error else "success",
        "quality_status": quality_status,
        "categories": categories,
        "refusal_reason": refusal_reason,
        "fallback": fallback,
        "citations_valid": citations_valid,
    }


def _llm_calls(trace: SearchTrace) -> int:
    details = _details(trace)
    measured = details.get("llm_calls")
    if isinstance(measured, int) and measured >= 0:
        return measured
    if trace.prompt_tokens is None and trace.completion_tokens is None:
        return 0
    return 1 + int(bool(details.get("citation_repair_attempted")))


def _nearest_rank_p95(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)])


def aggregate_traces(
    traces: Iterable[SearchTrace],
    feedback_by_trace: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    rows = list(traces)
    feedback_by_trace = feedback_by_trace or {}
    category_counts: defaultdict[str, int] = defaultdict(int)
    modes = {"baseline": 0, "agent": 0}
    latencies: list[int] = []
    input_tokens = 0
    output_tokens = 0
    token_rows = 0
    llm_calls = 0
    fallback_count = 0
    by_day: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"queries": 0, "technical_errors": 0, "manual_review_required": 0}
    )

    for trace in rows:
        result = classify_trace(trace, feedback_by_trace.get(str(trace.id), []))
        for category in result["categories"]:
            category_counts[category] += 1
        modes[trace_mode(trace)] += 1
        fallback_count += int(result["fallback"])
        llm_calls += _llm_calls(trace)
        if trace.latency_ms is not None:
            latencies.append(trace.latency_ms)
        if trace.prompt_tokens is not None or trace.completion_tokens is not None:
            token_rows += 1
            input_tokens += trace.prompt_tokens or 0
            output_tokens += trace.completion_tokens or 0
        day = trace.created_at.date().isoformat()
        by_day[day]["queries"] += 1
        by_day[day]["technical_errors"] += int("technical_error" in result["categories"])
        by_day[day]["manual_review_required"] += int(
            "manual_review_required" in result["categories"]
        )

    total = len(rows)
    average = round(sum(latencies) / len(latencies), 1) if latencies else None
    p95 = _nearest_rank_p95(latencies)
    return {
        "total_queries": total,
        "technical_errors": category_counts["technical_error"],
        "failed_queries": category_counts["technical_error"],
        "justified_abstentions": category_counts["justified_abstention"],
        "manual_review_required": category_counts["manual_review_required"],
        "average_latency_ms": average or 0,
        "p95_latency_ms": p95,
        "latency_measurement": "measured" if latencies else "not_measured",
        "llm_calls": llm_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_measurement": "measured" if token_rows else "not_measured",
        "modes": {
            **modes,
            "baseline_share": round(modes["baseline"] / total, 4) if total else 0,
            "agent_share": round(modes["agent"] / total, 4) if total else 0,
        },
        "fallback_count": fallback_count,
        "categories": dict(category_counts),
        "answer_correctness": "not_measured",
        "time_series": [
            {"date": day, **counts} for day, counts in sorted(by_day.items())
        ],
    }
