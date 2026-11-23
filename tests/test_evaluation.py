import json
from pathlib import Path

from clip_agent.evaluation import (
    aggregate_results,
    compare_runs,
    evaluate_case,
    load_dataset,
    source_id,
)


def case(**overrides):
    value = {
        "case_id": "T-1",
        "question": "Какая ставка?",
        "category": "single_document_fact",
        "expected_sources": ["example:order_101"],
        "required_facts": [
            {
                "fact_id": "rate",
                "description": "Ставка 0,10",
                "patterns": [r"0[,.]10? *(?:%|процент)"],
                "source_id": "example:order_101",
                "citation_required": True,
            }
        ],
        "forbidden_claims": [
            {"claim_id": "wrong", "description": "Неверная ставка", "pattern": r"0[,.]05"}
        ],
        "should_abstain": False,
        "notes": "test",
    }
    value.update(overrides)
    return value


def response():
    return {
        "answer": "Ставка составляет 0,10 процента [2].",
        "sources": [
            {"filename": "agency_contract.txt", "document_id": "d1", "chunk_id": "c1"},
            {"filename": "order_101.txt", "document_id": "d2", "chunk_id": "c2"},
        ],
        "metrics": {"latency_ms": 100, "prompt_tokens": 20, "completion_tokens": 5},
    }


def test_dataset_has_required_fields_and_stable_sources():
    path = Path("eval/datasets/corporate_v1.jsonl")
    cases = load_dataset(path)
    assert len(cases) >= 15
    assert len({item["case_id"] for item in cases}) == len(cases)
    assert source_id("Order_101.txt") == "example:order_101"
    assert all(source.startswith("example:") for item in cases for source in item["expected_sources"])


def test_loader_rejects_duplicate_ids(tmp_path):
    item = case()
    path = tmp_path / "duplicate.jsonl"
    path.write_text(
        json.dumps(item, ensure_ascii=False) + "\n" + json.dumps(item, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        load_dataset(path)
    except ValueError as exc:
        assert "duplicate case_id" in str(exc)
    else:
        raise AssertionError("duplicate case_id was accepted")


def test_retrieval_fact_and_citation_metrics():
    result = evaluate_case(case(), response())
    assert result["metrics"]["document_recall_at_5"] == 1.0
    assert result["metrics"]["mrr"] == 0.5
    assert result["metrics"]["citation_ids_valid"] is True
    assert result["metrics"]["citations_present"] is True
    assert result["metrics"]["citation_ids_admissible"] is True
    assert result["metrics"]["required_facts_passed"] is True
    assert result["metrics"]["forbidden_claims_clear"] is True


def test_invalid_citation_and_forbidden_claim_are_detected():
    payload = response()
    payload["answer"] = "Ставка 0,05 процента [9]."
    result = evaluate_case(case(), payload)
    assert result["metrics"]["citation_ids_valid"] is False
    assert result["metrics"]["citation_ids_admissible"] is False
    assert result["metrics"]["required_facts_passed"] is False
    assert result["metrics"]["forbidden_claims_clear"] is False


def test_explicit_insufficient_data_is_a_correct_abstention():
    abstention_case = case(
        category="insufficient_data",
        required_facts=[],
        forbidden_claims=[],
        should_abstain=True,
    )
    payload = response()
    payload["answer"] = "Данных в документах недостаточно для ответа [1]."
    result = evaluate_case(abstention_case, payload)
    assert result["metrics"]["abstention_detected"] is True
    assert result["metrics"]["abstention_correct"] is True
    assert result["manual_review_required"] is True


def test_aggregate_and_comparison_are_deterministic():
    first = evaluate_case(case(), response())
    second_response = response()
    second_response["metrics"]["latency_ms"] = 300
    second = evaluate_case(case(case_id="T-2"), second_response)
    summary = aggregate_results([first, second])
    assert summary["latency_ms"] == {"mean": 200.0, "median": 200.0, "p95": 300.0}
    assert summary["tokens"]["input_total"] == 40
    assert summary["abstention_accuracy"] is None
    run = {
        "experiment_id": "a",
        "dataset": {"id": "corporate", "version": "v1"},
        "corpus": {"hash": "abc"},
        "summary": summary,
    }
    changed = json.loads(json.dumps(run))
    changed["experiment_id"] = "b"
    changed["summary"]["mrr"] = 1.0
    comparison = compare_runs(run, changed)
    assert comparison["same_dataset"] is True
    assert comparison["metrics"]["mrr"]["delta"] == 0.5
