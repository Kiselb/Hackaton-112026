from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from clip_agent.analytics import aggregate_traces, classify_trace
from clip_agent.db import Base, Chunk, Document, SearchTrace, TraceFeedback
from clip_agent.main import (
    AskRequest,
    FeedbackRequest,
    _serialize_trace,
    ask,
    create_feedback,
    trace_detail,
    traces,
)


def make_trace(**overrides) -> SearchTrace:
    values = {
        "id": uuid4(),
        "question": "Какой срок установлен?",
        "user_id": "user-a",
        "session_id": "session-a",
        "answer": "Ответ [1].",
        "retrieved_count": 4,
        "selected_count": 1,
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "latency_ms": 100,
        "status": "ok",
        "details": {"mode": "baseline", "citations_valid": True, "llm_calls": 1},
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SearchTrace(**values)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def test_classification_separates_execution_from_quality_and_allows_overlap():
    trace = make_trace(
        retrieved_count=2,
        selected_count=1,
        details={
            "mode": "langgraph_agent",
            "gaps": [{"aspect_id": "a", "reason": "unconfirmed"}],
            "refusal_reason": "answer_validation_failed",
            "validation": {"citations_valid": False},
        },
    )

    result = classify_trace(trace)

    assert result["technical_status"] == "success"
    assert {
        "evidence_gap",
        "justified_abstention",
        "citation_problem",
        "manual_review_required",
    }.issubset(result["categories"])
    assert "technical_error" not in result["categories"]


def test_technical_success_does_not_claim_answer_correctness():
    result = classify_trace(make_trace())

    assert result["technical_status"] == "success"
    assert result["quality_status"] == "unknown"
    assert result["categories"] == ["answer_produced"]


def test_technical_failure_is_not_mislabeled_as_no_evidence_abstention():
    result = classify_trace(
        make_trace(
            status="failed",
            answer=None,
            retrieved_count=0,
            selected_count=0,
            details={"mode": "baseline", "error": "search unavailable"},
        )
    )

    assert result["categories"] == ["technical_error"]
    assert result["quality_status"] == "not_measured"


def test_aggregate_handles_empty_history_and_nearest_rank_p95():
    empty = aggregate_traces([])
    assert empty["total_queries"] == 0
    assert empty["p95_latency_ms"] is None
    assert empty["latency_measurement"] == "not_measured"
    assert empty["answer_correctness"] == "not_measured"

    started = datetime.now(UTC)
    rows = [
        make_trace(latency_ms=value, created_at=started + timedelta(minutes=index))
        for index, value in enumerate((100, 200, 900))
    ]
    rows[1].details = {
        "mode": "langgraph_agent",
        "llm_calls": 3,
        "validation": {"citations_valid": True},
    }
    summary = aggregate_traces(rows)

    assert summary["average_latency_ms"] == 400.0
    assert summary["p95_latency_ms"] == 900.0
    assert summary["llm_calls"] == 5
    assert summary["input_tokens"] == 30
    assert summary["output_tokens"] == 12
    assert summary["modes"]["baseline"] == 2
    assert summary["modes"]["agent"] == 1


def test_trace_list_filters_categories_and_paginates(db: Session):
    rows = [
        make_trace(question="one", created_at=datetime.now(UTC) - timedelta(minutes=2)),
        make_trace(
            question="two",
            retrieved_count=0,
            selected_count=0,
            answer="В активных документах не найдено достаточно данных для ответа.",
            details={"mode": "baseline", "citations_valid": False, "llm_calls": 0},
            created_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
        make_trace(question="three", status="failed", answer=None, details={"error": "secret"}),
    ]
    db.add_all(rows)
    db.commit()
    response = Response()

    page = traces(
        response=response,
        limit=1,
        offset=0,
        category="manual_review_required",
        db=db,
    )

    assert response.headers["X-Total-Count"] == "1"
    assert page[0]["question"] == "two"

    all_rows = traces(response=Response(), limit=10, offset=0, db=db)
    failed = next(item for item in all_rows if item["question"] == "three")
    assert failed["details"]["error"] == "recorded"


def test_feedback_requires_matching_trace_identity_and_is_not_truth_confirmation(db: Session):
    trace = make_trace()
    db.add(trace)
    db.commit()

    saved = create_feedback(
        trace.id,
        FeedbackRequest(rating="helpful", user_id="user-a", session_id="session-a"),
        db,
    )
    assert saved["rating"] == "helpful"
    assert db.query(TraceFeedback).count() == 1
    assert classify_trace(trace, ["helpful"])["quality_status"] == "unknown"

    with pytest.raises(HTTPException) as error:
        create_feedback(
            trace.id,
            FeedbackRequest(rating="incorrect", user_id="user-b", session_id="session-a"),
            db,
        )
    assert error.value.status_code == 404


def test_trace_detail_reconstructs_ordered_sources_and_agent_trajectory(db: Session):
    document = Document(
        id=uuid4(),
        title="Регламент",
        filename="rules.txt",
        active=True,
        status="ready",
        storage_key="test/rules.txt",
    )
    chunk = Chunk(
        id=uuid4(),
        document_id=document.id,
        ordinal=0,
        page=None,
        parent_text="До выделенный фрагмент после.",
        child_text="выделенный фрагмент",
        child_start=3,
        child_end=23,
    )
    trace = make_trace(
        details={
            "mode": "langgraph_agent",
            "source_ids": [str(chunk.id)],
            "trajectory": [
                {
                    "step": 1,
                    "node": "route_and_plan",
                    "decision": "fast_route",
                    "latency_ms": 0.1,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }
            ],
            "validation": {"citations_valid": True},
        }
    )
    db.add_all([document, chunk, trace])
    db.commit()

    result = trace_detail(trace.id, db)

    assert result["mode"] == "agent"
    assert result["details"]["trajectory"][0]["node"] == "route_and_plan"
    assert result["sources"][0]["chunk_id"] == str(chunk.id)
    assert result["sources"][0]["page"] is None


def test_serialized_baseline_is_never_labeled_as_agent():
    result = _serialize_trace(make_trace(details={"mode": "baseline"}))
    assert result["mode"] == "baseline"


def test_invalid_input_is_not_mislabeled_as_missing_evidence():
    result = classify_trace(
        make_trace(details={"mode": "langgraph_agent", "outcome": "invalid_input"})
    )

    assert result["outcome"] == "invalid_input"
    assert "no_evidence" not in result["categories"]
    assert "justified_abstention" in result["categories"]


def test_request_level_agent_override_is_disabled_by_default(monkeypatch):
    from clip_agent import main

    monkeypatch.setattr(main.settings, "agent_demo_override_enabled", False)

    with pytest.raises(HTTPException) as error:
        ask(
            AskRequest(question="Какой срок?", mode="agent"),
            db=object(),
        )

    assert error.value.status_code == 403
