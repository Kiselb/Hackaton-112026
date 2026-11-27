import json
from pathlib import Path

from clip_agent.evaluation import (
    aggregate_results,
    compare_runs,
    evaluate_case,
    load_dataset,
    source_id,
)
from scripts.evaluate import (
    annotate_sources,
    corpus_manifests_match,
    digest,
    load_corpus_config,
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
    assert result["metrics"]["document_recall_at_1"] == 0.0
    assert result["metrics"]["document_recall_at_3"] == 1.0
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


def test_glossary_evaluation_tracks_false_expansion_and_cost():
    payload = response()
    payload["metrics"].update(
        {
            "glossary_used": True,
            "glossary_query_changed": True,
            "glossary_found_terms": 1,
            "glossary_applied_expansions": 1,
            "glossary_skipped_expansions": 0,
            "glossary_additional_retrieval_calls": 0,
            "glossary_latency_ms": 0.25,
        }
    )
    result = evaluate_case(case(glossary_should_expand=False), payload)
    summary = aggregate_results([result])

    assert result["metrics"]["false_glossary_expansion"] is True
    assert summary["glossary"] == {
        "used_cases": 1,
        "found_terms_total": 1,
        "applied_expansions_total": 1,
        "skipped_expansions_total": 0,
        "false_expansion_rate": 1.0,
        "additional_retrieval_calls_total": 0,
        "latency_ms_mean": 0.25,
        "latency_ms_total": 0.25,
    }


def test_ndcg_fragment_and_all_source_coverage():
    multi = case(
        expected_sources=["example:order_101", "example:agency_contract"],
        expected_fragments=["f-order", "f-agency"],
        relevance_grades={"example:order_101": 3, "example:agency_contract": 2},
        required_facts=[],
    )
    payload = response()
    payload["sources"][0]["fragment_ids"] = ["f-agency"]
    payload["sources"][1]["fragment_ids"] = ["f-order"]
    result = evaluate_case(multi, payload)

    assert result["metrics"]["ndcg_at_5"] < 1.0
    assert result["metrics"]["all_required_sources_at_8"] is True
    assert result["metrics"]["expected_fragment_coverage"] == 1.0


def test_new_corpus_manifest_accepts_matching_legacy_manifest():
    files = {"one.txt": "abc"}
    legacy = {
        "hash": digest(json.dumps(files, sort_keys=True).encode()),
        "files": files,
    }
    current = {**legacy, "hash": "new", "config_sha256": "config"}

    assert corpus_manifests_match(legacy, current) is True
    assert corpus_manifests_match({**legacy, "files": {}}, current) is False


def test_document_metrics_deduplicate_sources_by_first_appearance():
    payload = response()
    payload["sources"] = [
        {"filename": "agency_contract.txt", "document_id": "d1", "chunk_id": "c1"},
        {"filename": "agency_contract.txt", "document_id": "d1", "chunk_id": "c2"},
        {"filename": "order_101.txt", "document_id": "d2", "chunk_id": "c3"},
    ]

    result = evaluate_case(case(required_facts=[]), payload)

    assert result["retrieved_source_occurrences"] == [
        "example:agency_contract",
        "example:agency_contract",
        "example:order_101",
    ]
    assert result["retrieved_sources"] == [
        "example:agency_contract",
        "example:order_101",
    ]
    assert result["metrics"]["mrr"] == 0.5
    assert result["metrics"]["document_recall_at_3"] == 1.0


def test_passage_metrics_use_retrieval_slots_and_fixed_k_denominator():
    payload = response()
    payload["sources"][0]["passage_ids"] = []
    payload["sources"][1]["passage_ids"] = ["passage:rate"]
    passage_case = case(
        expected_passages=["passage:rate"],
        expected_fragments=[],
        required_facts=[],
    )

    result = evaluate_case(passage_case, payload)

    assert result["metrics"]["passage_recall_at_1"] == 0.0
    assert result["metrics"]["passage_recall_at_3"] == 1.0
    assert result["metrics"]["passage_precision_at_3"] == 1 / 3
    assert result["metrics"]["passage_precision_at_5"] == 1 / 5
    assert result["metrics"]["passage_mrr"] == 0.5


def test_empty_gold_is_not_applicable_for_retrieval_metrics():
    empty = case(expected_sources=[], expected_passages=[], required_facts=[])

    result = evaluate_case(empty, response())

    assert result["metrics"]["document_recall_at_1"] is None
    assert result["metrics"]["document_precision_at_3"] is None
    assert result["metrics"]["mrr"] is None
    assert result["metrics"]["passage_recall_at_5"] is None
    assert result["metrics"]["passage_precision_at_5"] is None


def test_docx_corpus_config_validates_quotes_from_tables(tmp_path):
    from docx import Document

    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Обязательно"
    table.cell(0, 1).text = "Итоговый расчёт"
    source = tmp_path / "regulation.docx"
    document.save(source)
    config = tmp_path / "corpus.json"
    config.write_text(
        json.dumps(
            {
                "corpus_id": "docx-test",
                "version": "v1",
                "documents": [
                    {
                        "source_id": "docx:test",
                        "filename": source.name,
                        "title": "Регламент",
                        "path": source.name,
                        "passages": [
                            {
                                "passage_id": "docx:test:table",
                                "quote": "Обязательно | Итоговый расчёт",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    loaded = load_corpus_config(config)

    assert loaded["documents"][0]["passages"][0]["passage_id"] == "docx:test:table"


def test_passage_annotation_uses_parent_context_and_stable_id():
    config = {
        "documents": [
            {
                "source_id": "docx:test",
                "filename": "regulation.docx",
                "passages": [
                    {"passage_id": "docx:test:p1", "quote": "границу child chunks"}
                ],
            }
        ]
    }
    payload = {
        "sources": [
            {
                "filename": "regulation.docx",
                "parent_text": "Положение пересекает границу child chunks полностью.",
                "child_text": "Положение пересекает границу",
            },
            {
                "filename": "regulation.docx",
                "parent_text": "Положение пересекает границу child chunks полностью.",
                "child_text": "child chunks полностью.",
            },
        ]
    }

    annotate_sources(payload, config)

    assert payload["sources"][0]["passage_ids"] == ["docx:test:p1"]
    assert payload["sources"][0]["retrieval_passage_id"] == payload["sources"][1][
        "retrieval_passage_id"
    ]


def test_failed_case_is_counted_but_excluded_from_metric_denominators():
    completed = evaluate_case(case(), response())
    failed_payload = {"answer": "", "sources": [], "metrics": {}, "error": "boom"}
    failed = evaluate_case(case(case_id="T-failed"), failed_payload)

    summary = aggregate_results([completed, failed])

    assert summary["case_count"] == 2
    assert summary["completed_count"] == 1
    assert summary["error_count"] == 1
    assert summary["document_recall_at_1"] == 0.0
    assert summary["denominators"]["document_recall_at_1"] == 1
    assert summary["latency_ms"] == {"mean": 100.0, "median": 100, "p95": 100.0}


def test_comparison_rejects_different_dataset_or_corpus_and_preserves_gold_status():
    result = evaluate_case(
        case(gold_status="review_required", reviewer=None, reviewed_at=None), response()
    )
    assert result["gold_status"] == "review_required"

    summary = aggregate_results([result])
    left = {
        "experiment_id": "left",
        "dataset": {"sha256": "gold-v1"},
        "corpus": {"hash": "corpus-v1"},
        "summary": summary,
    }
    right = json.loads(json.dumps(left))
    right["experiment_id"] = "right"
    right["dataset"]["sha256"] = "gold-v2"
    right["corpus"]["hash"] = "corpus-v2"

    comparison = compare_runs(left, right)

    assert comparison["same_dataset"] is False
    assert comparison["same_corpus_hash"] is False
