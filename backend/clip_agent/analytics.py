from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

from .db import SearchTrace


FEEDBACK_RATINGS = {"helpful", "incomplete", "incorrect", "missing_documents"}
TECHNICAL_SUCCESS_STATUSES = {"ok"}
CONTENT_OUTCOMES = {
    "answered",
    "clarification_required",
    "quality_review_required",
    "conflict_detected",
    "out_of_scope",
    "invalid_input",
    "technical_error",
}
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
    recorded_outcome = details.get("outcome")
    outcome = recorded_outcome if recorded_outcome in CONTENT_OUTCOMES else None
    technical_error = trace.status not in TECHNICAL_SUCCESS_STATUSES or outcome == "technical_error"
    no_evidence = not technical_error and outcome not in {"out_of_scope", "invalid_input"} and (
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
    justified_abstention = refusal_reason in JUSTIFIED_REFUSAL_REASONS or outcome in {
        "clarification_required",
        "quality_review_required",
        "conflict_detected",
        "out_of_scope",
        "invalid_input",
    }
    manual_review = (
        no_evidence
        or evidence_gap
        or citation_problem
        or agent_fallback
        or outcome in {"clarification_required", "quality_review_required", "conflict_detected"}
        or bool(feedback & {"incomplete", "incorrect", "missing_documents"})
    )
    answer_produced = bool(trace.answer) and not technical_error and (
        outcome == "answered" if outcome else not justified_abstention
    )

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
        "outcome": outcome,
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
    conversation_ids: set[str] = set()
    clarification_count = 0
    confirmed_answers = 0
    quality_review_transitions = 0
    multi_query_requested = 0
    multi_query_not_requested = 0
    multi_query_successful_expansions = 0
    multi_query_fallbacks = 0
    multi_query_groups: dict[str, dict[str, Any]] = {
        "on": {
            "queries": 0,
            "retrieval_calls": 0,
            "latencies": [],
            "unique_sources": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "outcomes": defaultdict(int),
            "technical_errors": 0,
        },
        "off": {
            "queries": 0,
            "retrieval_calls": 0,
            "latencies": [],
            "unique_sources": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "outcomes": defaultdict(int),
            "technical_errors": 0,
        },
    }
    by_day: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"queries": 0, "technical_errors": 0, "manual_review_required": 0}
    )

    for trace in rows:
        result = classify_trace(trace, feedback_by_trace.get(str(trace.id), []))
        details = _details(trace)
        conversation_id = details.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            conversation_ids.add(conversation_id)
        clarification_count += int(result["outcome"] == "clarification_required")
        confirmed_answers += int(result["outcome"] == "answered")
        quality_review_transitions += int(result["outcome"] == "quality_review_required")
        if trace_mode(trace) == "agent":
            requested = bool(details.get("multi_query_requested"))
            effective = bool(details.get("multi_query_effective"))
            multi_query_requested += int(requested)
            multi_query_not_requested += int(not requested)
            multi_query_successful_expansions += int(
                effective and details.get("multi_query_status") == "applied"
            )
            multi_query_fallbacks += int(
                requested and not effective
                or details.get("multi_query_status") == "partial_retrieval"
            )
            group = multi_query_groups["on" if effective else "off"]
            group["queries"] += 1
            group["retrieval_calls"] += int(details.get("search_calls") or 0)
            group["unique_sources"] += int(
                details.get("retrieved_unique_chunks")
                or details.get("multi_query_unique_sources")
                or trace.selected_count
                or 0
            )
            group["input_tokens"] += trace.prompt_tokens or 0
            group["output_tokens"] += trace.completion_tokens or 0
            if trace.latency_ms is not None:
                group["latencies"].append(trace.latency_ms)
            group["outcomes"][result["outcome"] or "unknown"] += 1
            group["technical_errors"] += int(result["technical_status"] == "error")
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
    multi_query_summary: dict[str, dict[str, Any]] = {}
    for name, group in multi_query_groups.items():
        count = int(group["queries"])
        latencies_for_group = list(group["latencies"])
        outcomes = dict(group["outcomes"])
        multi_query_summary[name] = {
            "queries": count,
            "average_retrieval_calls": (
                round(group["retrieval_calls"] / count, 2) if count else 0
            ),
            "average_latency_ms": (
                round(sum(latencies_for_group) / len(latencies_for_group), 1)
                if latencies_for_group
                else None
            ),
            "p95_latency_ms": _nearest_rank_p95(latencies_for_group),
            "average_unique_sources": (
                round(group["unique_sources"] / count, 2) if count else 0
            ),
            "input_tokens": group["input_tokens"],
            "output_tokens": group["output_tokens"],
            "answered_share": round(outcomes.get("answered", 0) / count, 4) if count else 0,
            "quality_review_required_share": (
                round(outcomes.get("quality_review_required", 0) / count, 4)
                if count else 0
            ),
            "clarification_required_share": (
                round(outcomes.get("clarification_required", 0) / count, 4)
                if count else 0
            ),
            "conflict_detected_share": (
                round(outcomes.get("conflict_detected", 0) / count, 4)
                if count else 0
            ),
            "technical_errors": group["technical_errors"],
        }
    return {
        "total_queries": total,
        "total_conversations": len(conversation_ids),
        "user_messages": total,
        "clarifications": clarification_count,
        "confirmed_answers": confirmed_answers,
        "quality_review_transitions": quality_review_transitions,
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
        "multi_query": {
            "requested": multi_query_requested,
            "not_requested": multi_query_not_requested,
            "successful_expansions": multi_query_successful_expansions,
            "fallbacks": multi_query_fallbacks,
            "groups": multi_query_summary,
        },
        "categories": dict(category_counts),
        "answer_correctness": "not_measured",
        "time_series": [
            {"date": day, **counts} for day, counts in sorted(by_day.items())
        ],
    }
