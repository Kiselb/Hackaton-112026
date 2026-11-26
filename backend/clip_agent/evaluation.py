from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path
from typing import Any


ABSTENTION_PATTERNS = (
    r"недостаточно\s+(?:данных|информации|сведений)",
    r"(?:данн|информац|сведен)[^.]{0,120}недостаточ",
    r"(?:отсутствует|нет)[^.]{0,50}информац",
    r"(?:данные|сведения|информация)\s+(?:не\s+предоставлены|отсутствуют)",
    r"не(?:льзя|возможно)\s+(?:определить|установить|ответить|утверждать)",
    r"документ(?:ы|ах)?\s+не\s+(?:содержит|содержат|указан|указано)",
    r"нет\s+оснований",
)


def source_id(filename: str) -> str:
    return f"example:{Path(filename).stem.lower()}"


def load_dataset(path: Path) -> list[dict[str, Any]]:
    required = {
        "case_id", "question", "category", "expected_sources", "required_facts",
        "forbidden_claims", "should_abstain", "notes",
    }
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        case = json.loads(raw)
        if missing := required - case.keys():
            raise ValueError(f"dataset line {number}: missing fields {sorted(missing)}")
        if case["case_id"] in seen:
            raise ValueError(f"dataset line {number}: duplicate case_id {case['case_id']!r}")
        seen.add(case["case_id"])
        cases.append(case)
    if not cases:
        raise ValueError("dataset is empty")
    return cases


def _sentences(answer: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", answer) if part.strip()]


def _citation_numbers(answer: str) -> list[int]:
    return [int(value) for value in re.findall(r"\[(\d+)\]", answer)]


def _unique_by_first(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _fact_check(
    fact: dict[str, Any], answer: str, sources: list[dict[str, Any]]
) -> dict[str, Any]:
    patterns = fact.get("patterns", [])
    matches = [bool(re.search(pattern, answer, re.IGNORECASE)) for pattern in patterns]
    text_match = all(matches) if fact.get("mode", "all") == "all" else any(matches)
    citation_match: bool | None = None
    cited_sources: set[str] = set()
    if fact.get("citation_required"):
        for sentence in _sentences(answer):
            if not any(re.search(pattern, sentence, re.IGNORECASE) for pattern in patterns):
                continue
            for number in _citation_numbers(sentence):
                if 1 <= number <= len(sources):
                    cited_sources.add(sources[number - 1]["source_id"])
        citation_match = fact.get("source_id") in cited_sources
    return {
        "fact_id": fact["fact_id"],
        "description": fact["description"],
        "text_condition_met": text_match,
        "citation_condition_met": citation_match,
        "passed": text_match and citation_match is not False,
        "cited_sources": sorted(cited_sources),
        "scope": "deterministic lexical condition; not an entailment judgement",
    }


def evaluate_case(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    answer = response.get("answer", "")
    sources = [
        {**item, "source_id": item.get("source_id") or source_id(item["filename"])}
        for item in response.get("sources", [])
    ]
    retrieved_occurrences = [item["source_id"] for item in sources]
    retrieved = _unique_by_first(retrieved_occurrences)
    expected = list(case["expected_sources"])
    expected_set = set(expected)
    expected_fragments = set(case.get("expected_fragments", []))
    retrieved_passages = [
        passage_id
        for item in sources
        for passage_id in item.get("passage_ids", item.get("fragment_ids", []))
    ]
    expected_passages = set(
        case.get("expected_passages", case.get("expected_fragments", []))
    )

    def recall_at(k: int) -> float | None:
        if not expected_set:
            return None
        return len(expected_set.intersection(retrieved[:k])) / len(expected_set)

    def precision_at(k: int) -> float | None:
        if not expected_set:
            return None
        return len(expected_set.intersection(retrieved[:k])) / k

    first_rank = next(
        (rank for rank, item in enumerate(retrieved, start=1) if item in expected_set), None
    )
    relevance_grades = case.get("relevance_grades", {})

    def ndcg_at(k: int) -> float | None:
        if not relevance_grades:
            return None
        gains = [relevance_grades.get(item, 0) for item in retrieved[:k]]
        ideal = sorted(relevance_grades.values(), reverse=True)[:k]

        def dcg(values: list[int | float]) -> float:
            return sum(
                (2**grade - 1) / math.log2(rank + 1)
                for rank, grade in enumerate(values, start=1)
            )

        ideal_score = dcg(ideal)
        return dcg(gains) / ideal_score if ideal_score else None
    def passage_recall_at(k: int) -> float | None:
        if not expected_passages:
            return None
        covered = {
            passage_id
            for item in sources[:k]
            for passage_id in item.get("passage_ids", item.get("fragment_ids", []))
            if passage_id in expected_passages
        }
        return len(covered) / len(expected_passages)

    def passage_precision_at(k: int) -> float | None:
        if not expected_passages:
            return None
        relevant_slots = sum(
            bool(
                expected_passages.intersection(
                    item.get("passage_ids", item.get("fragment_ids", []))
                )
            )
            for item in sources[:k]
        )
        return relevant_slots / k

    first_passage_rank = next(
        (
            rank
            for rank, item in enumerate(sources, start=1)
            if expected_passages.intersection(
                item.get("passage_ids", item.get("fragment_ids", []))
            )
        ),
        None,
    )
    citation_numbers = _citation_numbers(answer)
    citations_present = bool(citation_numbers)
    citation_ids_admissible = all(
        1 <= number <= len(sources) for number in citation_numbers
    )
    citation_ids_valid = citations_present and citation_ids_admissible
    required_checks = [_fact_check(fact, answer, sources) for fact in case["required_facts"]]
    forbidden_checks = [
        {
            "claim_id": claim["claim_id"],
            "description": claim["description"],
            "detected": bool(re.search(claim["pattern"], answer, re.IGNORECASE)),
        }
        for claim in case["forbidden_claims"]
    ]
    abstention_detected = any(
        re.search(pattern, answer, re.IGNORECASE) for pattern in ABSTENTION_PATTERNS
    )
    should_abstain = bool(case["should_abstain"])
    forbidden_clear = not any(item["detected"] for item in forbidden_checks)
    abstention_correct = (
        abstention_detected and forbidden_clear if should_abstain else not abstention_detected
    )
    dialogue = response.get("dialogue") or {}
    outcome = response.get("outcome")
    initial_outcome = dialogue.get("initial_outcome", outcome)
    final_outcome = dialogue.get("final_outcome", outcome)
    expected_outcome = case.get(
        "expected_outcome", "quality_review_required" if should_abstain else "answered"
    )
    expected_final_outcome = case.get("dialogue", {}).get(
        "expected_final_outcome", expected_outcome
    )
    requires_clarification = bool(case.get("requires_clarification", False))
    clarification_question = str(dialogue.get("clarification_question") or "")
    clarification_success = (
        initial_outcome == "clarification_required"
        and "?" in clarification_question
        and final_outcome == expected_final_outcome
        if requires_clarification
        else None
    )
    required_facts_coverage = (
        sum(item["text_condition_met"] for item in required_checks) / len(required_checks)
        if required_checks
        else None
    )
    citation_fact_checks = [
        item
        for item, fact in zip(required_checks, case["required_facts"], strict=True)
        if fact.get("citation_required")
    ]
    citation_support_rate = (
        sum(bool(item["citation_condition_met"]) for item in citation_fact_checks)
        / len(citation_fact_checks)
        if citation_fact_checks
        else None
    )
    forbidden_claims_rate = (
        sum(item["detected"] for item in forbidden_checks) / len(forbidden_checks)
        if forbidden_checks
        else 0.0
    )
    expected_answered = expected_final_outcome == "answered"
    grounded_answer = (
        final_outcome == "answered"
        and (all(item["passed"] for item in required_checks) if required_checks else True)
        and forbidden_clear
        and citation_ids_valid
        if expected_answered
        else None
    )
    correct_abstention = (
        final_outcome in {"quality_review_required", "conflict_detected"}
        and forbidden_clear
        if should_abstain
        else None
    )
    conflict_handling_quality = (
        final_outcome == "conflict_detected" and forbidden_clear
        if expected_final_outcome == "conflict_detected"
        else None
    )
    manual_review_required = bool(
        case.get("manual_review_required", False)
        or case["category"] in {"multi_document", "conflict", "insufficient_data"}
        or should_abstain
        or case.get("gold_status", "draft") != "approved"
    )
    metrics = response.get("metrics", {})
    return {
        "case_id": case["case_id"],
        "question": case["question"],
        "category": case["category"],
        "answer": answer,
        "expected_sources": expected,
        "retrieved_sources": retrieved,
        "retrieved_source_occurrences": retrieved_occurrences,
        "expected_passages": sorted(expected_passages),
        "retrieved_passages": _unique_by_first(retrieved_passages),
        "retrieved_documents": [
            {
                "rank": rank,
                "source_id": item["source_id"],
                "document_id": item.get("document_id"),
                "chunk_id": item.get("chunk_id"),
                "filename": item.get("filename"),
                "score": item.get("score"),
                "rrf_score": item.get("rrf_score"),
                "reranker_score": item.get("reranker_score"),
                "rank_before_rerank": item.get("rank_before_rerank"),
                "rank_after_rerank": item.get("rank_after_rerank"),
                "fragment_ids": item.get("fragment_ids", []),
                "passage_ids": item.get("passage_ids", item.get("fragment_ids", [])),
                "retrieval_passage_id": item.get("retrieval_passage_id"),
            }
            for rank, item in enumerate(sources, start=1)
        ],
        "retrieval_trace": response.get("retrieval"),
        "metrics": {
            "document_recall_at_1": recall_at(1),
            "document_recall_at_3": recall_at(3),
            "document_recall_at_5": recall_at(5),
            "document_recall_at_8": recall_at(8),
            "document_precision_at_1": precision_at(1),
            "document_precision_at_3": precision_at(3),
            "document_precision_at_5": precision_at(5),
            "document_precision_at_8": precision_at(8),
            "mrr": 1 / first_rank if first_rank else (0.0 if expected_set else None),
            "ndcg_at_3": ndcg_at(3),
            "ndcg_at_5": ndcg_at(5),
            "expected_document_coverage": recall_at(len(retrieved)),
            "all_required_sources_at_8": (
                expected_set.issubset(set(retrieved[:8])) if expected_set else None
            ),
            "expected_fragment_coverage": (
                len(expected_fragments.intersection(retrieved_passages)) / len(expected_fragments)
                if expected_fragments
                else None
            ),
            "passage_recall_at_1": passage_recall_at(1),
            "passage_recall_at_3": passage_recall_at(3),
            "passage_recall_at_5": passage_recall_at(5),
            "passage_recall_at_8": passage_recall_at(8),
            "passage_precision_at_3": passage_precision_at(3),
            "passage_precision_at_5": passage_precision_at(5),
            "passage_mrr": (
                1 / first_passage_rank
                if first_passage_rank
                else (0.0 if expected_passages else None)
            ),
            "citation_numbers": citation_numbers,
            "citations_present": citations_present,
            "citation_ids_admissible": citation_ids_admissible,
            "citation_ids_valid": citation_ids_valid,
            "required_facts": required_checks,
            "required_facts_passed": (
                all(item["passed"] for item in required_checks) if required_checks else None
            ),
            "required_facts_coverage": required_facts_coverage,
            "citation_support_rate": citation_support_rate,
            "forbidden_claims": forbidden_checks,
            "forbidden_claims_clear": forbidden_clear,
            "forbidden_claims_rate": forbidden_claims_rate,
            "should_abstain": should_abstain,
            "abstention_detected": abstention_detected,
            "abstention_correct": abstention_correct,
            "correct_abstention": correct_abstention,
            "grounded_answer": grounded_answer,
            "outcome": final_outcome,
            "expected_outcome": expected_final_outcome,
            "outcome_correct": final_outcome == expected_final_outcome,
            "clarification_success": clarification_success,
            "conflict_handling_quality": conflict_handling_quality,
            "latency_ms": metrics.get("latency_ms"),
            "input_tokens": metrics.get("prompt_tokens"),
            "output_tokens": metrics.get("completion_tokens"),
            "rerank_latency_ms": metrics.get("rerank_latency_ms"),
            "agent_search_calls": metrics.get("agent_search_calls", 1),
            "agent_llm_calls": metrics.get("agent_llm_calls", 1),
            "agent_steps": metrics.get("agent_steps"),
            "multi_query_effective": metrics.get("multi_query_effective", False),
            "multi_query_search_calls": metrics.get("multi_query_search_calls", 0),
            "multi_query_unique_sources": metrics.get("multi_query_unique_sources", 0),
        },
        "manual_review_required": manual_review_required,
        "manual_review_reason": (
            "Semantic correctness and citation support require human review; automatic checks are "
            "limited to explicit deterministic conditions."
            if manual_review_required
            else None
        ),
        "error": response.get("error"),
        "notes": case["notes"],
        "gold_status": case.get("gold_status", "draft"),
        "reviewer": case.get("reviewer"),
        "reviewed_at": case.get("reviewed_at"),
        "dialogue": dialogue or None,
        "trace_ids": dialogue.get("trace_ids", [response.get("trace_id")]),
    }


def _mean(values: list[float | int]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _p95(values: list[float | int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)])


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    def values(name: str) -> list[float | int]:
        return [
            item["metrics"][name]
            for item in results
            if item.get("error") is None and item["metrics"].get(name) is not None
        ]

    latencies = values("latency_ms")
    return {
        "case_count": len(results),
        "completed_count": sum(item.get("error") is None for item in results),
        "error_count": sum(item.get("error") is not None for item in results),
        "document_recall_at_1": _mean(values("document_recall_at_1")),
        "document_recall_at_3": _mean(values("document_recall_at_3")),
        "document_recall_at_5": _mean(values("document_recall_at_5")),
        "document_recall_at_8": _mean(values("document_recall_at_8")),
        "document_precision_at_1": _mean(values("document_precision_at_1")),
        "document_precision_at_3": _mean(values("document_precision_at_3")),
        "document_precision_at_5": _mean(values("document_precision_at_5")),
        "document_precision_at_8": _mean(values("document_precision_at_8")),
        "mrr": _mean(values("mrr")),
        "ndcg_at_3": _mean(values("ndcg_at_3")),
        "ndcg_at_5": _mean(values("ndcg_at_5")),
        "expected_document_coverage": _mean(values("expected_document_coverage")),
        "all_required_sources_at_8_rate": _mean(
            [float(v) for v in values("all_required_sources_at_8")]
        ),
        "expected_fragment_coverage": _mean(values("expected_fragment_coverage")),
        "passage_recall_at_1": _mean(values("passage_recall_at_1")),
        "passage_recall_at_3": _mean(values("passage_recall_at_3")),
        "passage_recall_at_5": _mean(values("passage_recall_at_5")),
        "passage_recall_at_8": _mean(values("passage_recall_at_8")),
        "passage_precision_at_3": _mean(values("passage_precision_at_3")),
        "passage_precision_at_5": _mean(values("passage_precision_at_5")),
        "passage_mrr": _mean(values("passage_mrr")),
        "citation_ids_valid_rate": _mean([float(v) for v in values("citation_ids_valid")]),
        "citations_present_rate": _mean([float(v) for v in values("citations_present")]),
        "citation_ids_admissible_rate": _mean(
            [float(v) for v in values("citation_ids_admissible")]
        ),
        "required_facts_pass_rate": _mean(
            [float(v) for v in values("required_facts_passed")]
        ),
        "required_facts_coverage": _mean(values("required_facts_coverage")),
        "citation_support_rate": _mean(values("citation_support_rate")),
        "forbidden_claims_clear_rate": _mean(
            [float(v) for v in values("forbidden_claims_clear")]
        ),
        "forbidden_claims_rate": _mean(values("forbidden_claims_rate")),
        "abstention_accuracy": _mean(
            [
                float(item["metrics"]["abstention_correct"])
                for item in results
                if item.get("error") is None and item["metrics"]["should_abstain"]
            ]
        ),
        "correct_abstention_rate": _mean(
            [float(v) for v in values("correct_abstention")]
        ),
        "grounded_answer_rate": _mean([float(v) for v in values("grounded_answer")]),
        "outcome_accuracy": _mean([float(v) for v in values("outcome_correct")]),
        "clarification_quality": _mean(
            [float(v) for v in values("clarification_success")]
        ),
        "conflict_handling_quality": _mean(
            [float(v) for v in values("conflict_handling_quality")]
        ),
        "manual_review_count": sum(item["manual_review_required"] for item in results),
        "latency_ms": {
            "mean": _mean(latencies),
            "median": round(statistics.median(latencies), 4) if latencies else None,
            "p95": _p95(latencies),
        },
        "rerank_latency_ms": {
            "mean": _mean(values("rerank_latency_ms")),
            "total": round(sum(values("rerank_latency_ms")), 4),
        },
        "tokens": {
            "input_total": sum(values("input_tokens")),
            "output_total": sum(values("output_tokens")),
            "input_mean": _mean(values("input_tokens")),
            "output_mean": _mean(values("output_tokens")),
        },
        "agent_calls": {
            "search_total": sum(values("agent_search_calls")),
            "search_mean": _mean(values("agent_search_calls")),
            "llm_total": sum(values("agent_llm_calls")),
            "llm_mean": _mean(values("agent_llm_calls")),
            "steps_mean": _mean(values("agent_steps")),
        },
        "denominators": {
            name: len(values(name))
            for name in (
                "document_recall_at_1",
                "document_precision_at_3",
                "passage_recall_at_5",
                "passage_precision_at_5",
                "mrr",
                "ndcg_at_3",
                "grounded_answer",
                "citation_support_rate",
                "correct_abstention",
                "clarification_success",
                "conflict_handling_quality",
            )
        },
        "aggregation": {
            "rule": "macro mean over completed applicable cases",
            "precision_short_list_denominator": "fixed K",
            "empty_gold": "not_applicable",
            "document_ranking": "unique source_id by first appearance",
            "passage_ranking": "retrieval slots with stable gold passage coverage",
        },
    }


def compare_runs(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "document_recall_at_1", "document_recall_at_3", "document_recall_at_5",
        "document_recall_at_8", "mrr", "ndcg_at_3", "ndcg_at_5", "expected_document_coverage",
        "document_precision_at_1", "document_precision_at_3",
        "document_precision_at_5", "document_precision_at_8",
        "all_required_sources_at_8_rate", "expected_fragment_coverage",
        "passage_recall_at_1", "passage_recall_at_3", "passage_recall_at_5",
        "passage_recall_at_8", "passage_precision_at_3", "passage_precision_at_5",
        "passage_mrr",
        "citations_present_rate",
        "citation_ids_admissible_rate", "citation_ids_valid_rate",
        "required_facts_pass_rate", "forbidden_claims_clear_rate",
        "required_facts_coverage", "citation_support_rate", "forbidden_claims_rate",
        "abstention_accuracy", "correct_abstention_rate", "grounded_answer_rate",
        "outcome_accuracy", "clarification_quality", "conflict_handling_quality",
    )
    left_summary, right_summary = left["summary"], right["summary"]
    metrics: dict[str, Any] = {}
    for key in keys:
        before, after = left_summary.get(key), right_summary.get(key)
        metrics[key] = {
            "left": before,
            "right": after,
            "delta": round(after - before, 4) if before is not None and after is not None else None,
        }
    for key in ("mean", "median", "p95"):
        before = left_summary["latency_ms"].get(key)
        after = right_summary["latency_ms"].get(key)
        metrics[f"latency_ms_{key}"] = {
            "left": before,
            "right": after,
            "delta": round(after - before, 4) if before is not None and after is not None else None,
        }
    before = left_summary.get("rerank_latency_ms", {}).get("mean")
    after = right_summary.get("rerank_latency_ms", {}).get("mean")
    metrics["rerank_latency_ms_mean"] = {
        "left": before,
        "right": after,
        "delta": round(after - before, 4) if before is not None and after is not None else None,
    }
    for key in ("search_mean", "llm_mean", "steps_mean"):
        before = left_summary.get("agent_calls", {}).get(key)
        after = right_summary.get("agent_calls", {}).get(key)
        metrics[f"agent_{key}"] = {
            "left": before,
            "right": after,
            "delta": round(after - before, 4) if before is not None and after is not None else None,
        }
    return {
        "left_experiment_id": left["experiment_id"],
        "right_experiment_id": right["experiment_id"],
        "same_dataset": left["dataset"] == right["dataset"],
        "same_corpus_hash": left["corpus"]["hash"] == right["corpus"]["hash"],
        "metrics": metrics,
    }
