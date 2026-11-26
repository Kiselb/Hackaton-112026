from pathlib import Path

from scripts.evaluate_multi_query_synthetic import load_cases, run


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval" / "datasets" / "multi_query_synthetic_v1.jsonl"


def test_multi_query_synthetic_dataset_is_versioned_and_complete():
    cases = load_cases(DATASET)

    assert len(cases) == 4
    assert all(case["scope"] and case["required_outcome"] for case in cases)
    assert all(case["complexity"] and case["ambiguity_reason"] for case in cases)


def test_multi_query_synthetic_comparison_is_reproducible():
    first = run(DATASET)
    second = run(DATASET)

    assert first == second
    assert first["on"]["summary"]["recall_at_3"] >= first["off"]["summary"]["recall_at_3"]
    assert first["on"]["summary"]["mrr"] > first["off"]["summary"]["mrr"]
    assert first["delta"]["average_latency_ms"] > 0
    assert first["real_corpus_quality"] == "manual_review_required"
