from types import SimpleNamespace
from uuid import uuid4

import pytest

from clip_agent.reranking import rerank_candidates
from clip_agent.search import build_active_candidates


def candidates() -> list[dict]:
    return [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "child_text": "первый фрагмент",
            "rrf_score": 0.03,
            "rank_before_rerank": 1,
        },
        {
            "chunk_id": "c2",
            "document_id": "d2",
            "child_text": "второй фрагмент",
            "rrf_score": 0.02,
            "rank_before_rerank": 2,
        },
    ]


def test_reranker_reorders_and_preserves_document_link():
    ranked, trace = rerank_candidates(
        "вопрос",
        candidates(),
        enabled=True,
        score_function=lambda question, items: [0.1, 0.9],
    )

    assert [item["chunk_id"] for item in ranked] == ["c2", "c1"]
    assert ranked[0]["document_id"] == "d2"
    assert ranked[0]["rrf_score"] == 0.02
    assert ranked[0]["reranker_score"] == 0.9
    assert ranked[0]["rank_before_rerank"] == 2
    assert ranked[0]["rank_after_rerank"] == 1
    assert trace["status"] == "ok"


def test_equal_scores_keep_rrf_order():
    ranked, _ = rerank_candidates(
        "вопрос",
        candidates(),
        enabled=True,
        score_function=lambda question, items: [0.5, 0.5],
    )

    assert [item["chunk_id"] for item in ranked] == ["c1", "c2"]


def test_empty_and_disabled_reranker_do_not_call_model():
    empty, empty_trace = rerank_candidates("вопрос", [], enabled=True)
    disabled, disabled_trace = rerank_candidates(
        "вопрос",
        candidates(),
        enabled=False,
        score_function=lambda question, items: pytest.fail("must not be called"),
    )

    assert empty == []
    assert empty_trace["status"] == "skipped_empty"
    assert [item["chunk_id"] for item in disabled] == ["c1", "c2"]
    assert all(item["reranker_score"] is None for item in disabled)
    assert disabled_trace["status"] == "disabled"


@pytest.mark.parametrize("error", [OSError("load failed"), RuntimeError("inference failed")])
def test_reranker_failure_falls_back_to_rrf(error):
    def fail(question, items):
        raise error

    ranked, trace = rerank_candidates(
        "вопрос", candidates(), enabled=True, score_function=fail
    )

    assert [item["chunk_id"] for item in ranked] == ["c1", "c2"]
    assert [item["score"] for item in ranked] == [0.03, 0.02]
    assert trace["status"] == "fallback"
    assert str(error) in trace["fallback_reason"]


def _row(active: bool, status: str = "ready"):
    document_id, chunk_id = uuid4(), uuid4()
    document = SimpleNamespace(
        id=document_id,
        title="Документ",
        filename=f"{document_id}.txt",
        active=active,
        status=status,
    )
    chunk = SimpleNamespace(
        id=chunk_id,
        document_id=document_id,
        page=None,
        parent_text="parent",
        child_text="child",
        child_start=0,
        child_end=5,
    )
    return chunk, document


def test_inactive_or_not_ready_document_cannot_become_candidate():
    active = _row(True)
    inactive = _row(False)
    failed = _row(True, status="failed")
    fused = [
        (str(inactive[0].id), 0.04),
        (str(failed[0].id), 0.03),
        (str(active[0].id), 0.02),
    ]

    result, by_id = build_active_candidates(
        fused, [inactive, failed, active], candidate_cap=3
    )

    assert [item["chunk_id"] for item in result] == [str(active[0].id)]
    assert set(by_id) == {str(active[0].id)}
    assert result[0]["rank_before_rerank"] == 3
