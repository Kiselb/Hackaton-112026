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
    retrieved = [item["source_id"] for item in sources]
    expected = list(case["expected_sources"])
    expected_set = set(expected)

    def recall_at(k: int) -> float | None:
        if not expected_set:
            return None
        return len(expected_set.intersection(retrieved[:k])) / len(expected_set)

    first_rank = next(
        (rank for rank, item in enumerate(retrieved, start=1) if item in expected_set), None
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
    manual_review_required = bool(
        case.get("manual_review_required", False)
        or case["category"] in {"multi_document", "conflict", "insufficient_data"}
        or should_abstain
    )
    metrics = response.get("metrics", {})
    return {
        "case_id": case["case_id"],
        "question": case["question"],
        "category": case["category"],
        "answer": answer,
        "expected_sources": expected,
        "retrieved_sources": retrieved,
        "retrieved_documents": [
            {
                "rank": rank,
                "source_id": item["source_id"],
                "document_id": item.get("document_id"),
                "chunk_id": item.get("chunk_id"),
                "filename": item.get("filename"),
                "score": item.get("score"),
            }
            for rank, item in enumerate(sources, start=1)
        ],
        "metrics": {
            "document_recall_at_5": recall_at(5),
            "document_recall_at_8": recall_at(8),
            "mrr": 1 / first_rank if first_rank else (0.0 if expected_set else None),
            "expected_document_coverage": recall_at(len(retrieved)),
            "citation_numbers": citation_numbers,
            "citations_present": citations_present,
            "citation_ids_admissible": citation_ids_admissible,
            "citation_ids_valid": citation_ids_valid,
            "required_facts": required_checks,
            "required_facts_passed": (
                all(item["passed"] for item in required_checks) if required_checks else None
            ),
            "forbidden_claims": forbidden_checks,
            "forbidden_claims_clear": forbidden_clear,
            "should_abstain": should_abstain,
            "abstention_detected": abstention_detected,
            "abstention_correct": abstention_correct,
            "latency_ms": metrics.get("latency_ms"),
            "input_tokens": metrics.get("prompt_tokens"),
            "output_tokens": metrics.get("completion_tokens"),
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
        "document_recall_at_5": _mean(values("document_recall_at_5")),
        "document_recall_at_8": _mean(values("document_recall_at_8")),
        "mrr": _mean(values("mrr")),
        "expected_document_coverage": _mean(values("expected_document_coverage")),
        "citation_ids_valid_rate": _mean([float(v) for v in values("citation_ids_valid")]),
        "citations_present_rate": _mean([float(v) for v in values("citations_present")]),
        "citation_ids_admissible_rate": _mean(
            [float(v) for v in values("citation_ids_admissible")]
        ),
        "required_facts_pass_rate": _mean(
            [float(v) for v in values("required_facts_passed")]
        ),
        "forbidden_claims_clear_rate": _mean(
            [float(v) for v in values("forbidden_claims_clear")]
        ),
        "abstention_accuracy": _mean(
            [
                float(item["metrics"]["abstention_correct"])
                for item in results
                if item.get("error") is None and item["metrics"]["should_abstain"]
            ]
        ),
        "manual_review_count": sum(item["manual_review_required"] for item in results),
        "latency_ms": {
            "mean": _mean(latencies),
            "median": round(statistics.median(latencies), 4) if latencies else None,
            "p95": _p95(latencies),
        },
        "tokens": {
            "input_total": sum(values("input_tokens")),
            "output_total": sum(values("output_tokens")),
            "input_mean": _mean(values("input_tokens")),
            "output_mean": _mean(values("output_tokens")),
        },
    }


def compare_runs(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "document_recall_at_5", "document_recall_at_8", "mrr",
        "expected_document_coverage", "citations_present_rate",
        "citation_ids_admissible_rate", "citation_ids_valid_rate",
        "required_facts_pass_rate", "forbidden_claims_clear_rate",
        "abstention_accuracy",
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
    return {
        "left_experiment_id": left["experiment_id"],
        "right_experiment_id": right["experiment_id"],
        "same_dataset": left["dataset"] == right["dataset"],
        "same_corpus_hash": left["corpus"]["hash"] == right["corpus"]["hash"],
        "metrics": metrics,
    }
