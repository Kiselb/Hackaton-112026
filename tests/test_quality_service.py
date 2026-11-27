from __future__ import annotations

import smtplib
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clip_agent import quality, search
from clip_agent.config import Settings
from clip_agent.conversations import hash_owner_token
from clip_agent.db import (
    Base,
    Conversation,
    ConversationTurn,
    Document,
    QualityKnowledgeEntry,
    QualityRequest,
    SearchTrace,
    utcnow,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        storage_root=tmp_path,
        quality_approval_token=SecretStr("quality-secret"),
        quality_request_rate_limit_per_hour=10,
    )


def seed_escalation(db: Session, *, outcome: str = "quality_review_required") -> tuple[SearchTrace, str]:
    token = "conversation-secret"
    conversation = Conversation(
        owner_token_hash=hash_owner_token(token),
        owner_id="browser-1",
        status="completed",
        mode="agent",
        original_question="Как оформить синтетическую заявку?",
        clarification_context={"answers": ["Для тестового подразделения"]},
        clarification_count=1,
        user_answer_count=1,
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(conversation)
    db.flush()
    trace = SearchTrace(
        question=conversation.original_question,
        user_id="demo",
        session_id="browser-1",
        answer="Предлагаю обратиться в службу качества.",
        status="ok",
        details={
            "outcome": outcome,
            "reason_code": "no_evidence",
            "conversation_id": str(conversation.id),
            "original_question": conversation.original_question,
            "clarification_answers": ["Для тестового подразделения"],
            "clarification_count": 1,
        },
    )
    db.add(trace)
    db.flush()
    db.add(
        ConversationTurn(
            conversation_id=conversation.id,
            trace_id=trace.id,
            ordinal=1,
            role="assistant",
            kind="final_answer",
            content=trace.answer or "",
            outcome=outcome,
        )
    )
    db.commit()
    return trace, token


def approve_request(db: Session, request: QualityRequest) -> QualityKnowledgeEntry:
    quality.save_draft(
        db,
        request,
        title="Порядок синтетической заявки",
        answer="Синтетическую заявку создаёт руководитель тестового подразделения.",
        scope="Только тестовое подразделение.",
        conditions="После согласования с владельцем процесса.",
        expert_label="jury-demo",
    )
    _, entry = quality.approve_draft(db, request.id)
    return entry


def test_request_requires_eligible_owned_trace_and_is_idempotent(db, app_settings):
    trace, token = seed_escalation(db)
    request, created = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-0001",
        conversation_token=token,
        settings=app_settings,
    )
    repeated, repeated_created = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-0001",
        conversation_token=token,
        settings=app_settings,
    )

    assert created is True
    assert repeated_created is False
    assert repeated.id == request.id
    assert request.email_status == "not_requested"
    assert request.clarification_context["answers"] == ["Для тестового подразделения"]
    assert len(db.scalars(select(QualityRequest)).all()) == 1
    with pytest.raises(quality.QualityServiceError) as denied_repeat:
        quality.create_quality_request(
            db,
            trace_id=trace.id,
            idempotency_key="idem-quality-0001",
            conversation_token="wrong",
            settings=app_settings,
        )
    assert denied_repeat.value.status_code == 404


def test_request_for_previous_trace_keeps_that_turn_question(db, app_settings):
    trace, token = seed_escalation(db)
    conversation = db.get(Conversation, UUID(trace.details["conversation_id"]))
    conversation.original_question = "Более поздний вопрос того же диалога"
    conversation.clarification_context = {"answers": []}
    db.commit()

    request, created = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-previous-turn",
        conversation_token=token,
        settings=app_settings,
    )

    assert created is True
    assert request.question == "Как оформить синтетическую заявку?"
    assert request.clarification_context["answers"] == ["Для тестового подразделения"]


def test_wrong_conversation_token_and_technical_error_are_blocked(db, app_settings):
    trace, _ = seed_escalation(db)
    with pytest.raises(quality.QualityServiceError) as denied:
        quality.create_quality_request(
            db,
            trace_id=trace.id,
            idempotency_key="idem-quality-0002",
            conversation_token="wrong",
            settings=app_settings,
        )
    assert denied.value.status_code == 404

    technical, token = seed_escalation(db, outcome="technical_error")
    with pytest.raises(quality.QualityServiceError) as ineligible:
        quality.create_quality_request(
            db,
            trace_id=technical.id,
            idempotency_key="idem-quality-0003",
            conversation_token=token,
            settings=app_settings,
        )
    assert ineligible.value.status_code == 409


def test_email_uses_only_server_recipient_and_mock_transport(db, app_settings, monkeypatch):
    trace, token = seed_escalation(db)
    configured = app_settings.model_copy(
        update={
            "quality_email_enabled": True,
            "quality_email_to": "quality@example.test",
            "quality_smtp_from": "clip@example.test",
            "quality_smtp_host": "smtp.example.test",
            "quality_smtp_password": SecretStr("smtp-password"),
        }
    )
    captured = []
    monkeypatch.setattr(quality, "_send_message", lambda message, settings: captured.append(message))

    request, _ = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-email",
        conversation_token=token,
        settings=configured,
    )

    assert request.email_status == "sent"
    assert captured[0]["To"] == "quality@example.test"
    content = captured[0].get_content()
    assert request.public_number in content
    assert request.question in content
    assert request.reason_code in content
    assert "Для тестового подразделения" in content
    assert "Откройте вкладку" in content
    serialized = quality.serialize_request(db, request, detail=True)
    assert "smtp-password" not in str(serialized)
    assert "quality@example.test" not in str(serialized)


def test_smtp_failure_preserves_request_and_unknown_is_not_retryable(
    db, app_settings, monkeypatch
):
    trace, token = seed_escalation(db)
    configured = app_settings.model_copy(
        update={
            "quality_email_enabled": True,
            "quality_email_to": "quality@example.test",
            "quality_smtp_from": "clip@example.test",
            "quality_smtp_host": "smtp.example.test",
        }
    )
    monkeypatch.setattr(
        quality,
        "_send_message",
        lambda *_: (_ for _ in ()).throw(smtplib.SMTPServerDisconnected("timeout")),
    )
    request, _ = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-unknown",
        conversation_token=token,
        settings=configured,
    )

    assert request.email_status == "delivery_unknown"
    assert db.get(QualityRequest, request.id) is not None


def test_approval_token_and_immutable_approved_version(db, app_settings):
    trace, token = seed_escalation(db)
    request, _ = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-approve",
        conversation_token=token,
        settings=app_settings,
    )
    with pytest.raises(quality.QualityServiceError):
        quality.require_approval_token("wrong", app_settings)
    quality.require_approval_token("quality-secret", app_settings)
    entry = approve_request(db, request)

    assert entry.status == "approved"
    assert entry.checksum
    entry.approved_text = "Переписанный задним числом текст"
    with pytest.raises(ValueError, match="immutable"):
        db.commit()
    db.rollback()


def test_publish_reuses_rag_document_and_is_idempotent(db, app_settings, monkeypatch):
    trace, token = seed_escalation(db)
    request, _ = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-publish",
        conversation_token=token,
        settings=app_settings,
    )
    entry = approve_request(db, request)
    calls = []
    monkeypatch.setattr(quality, "index_chunks", lambda chunks: calls.append(list(chunks)))

    published_request, published = quality.publish_latest(db, request.id, app_settings)
    repeated_request, repeated = quality.publish_latest(db, request.id, app_settings)
    document = db.get(Document, published.document_id)

    assert published_request.status == "published"
    assert repeated_request.id == published_request.id
    assert repeated.id == published.id == entry.id
    assert len(calls) == 1
    assert document is not None and document.active is True and document.status == "ready"
    assert "экспертное разъяснение" in document.chunks[0].parent_text.lower()


def test_indexing_failure_keeps_source_invisible_and_retry_reuses_document(
    db, app_settings, monkeypatch
):
    trace, token = seed_escalation(db)
    request, _ = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-failure",
        conversation_token=token,
        settings=app_settings,
    )
    approve_request(db, request)
    attempts = iter([RuntimeError("synthetic index failure"), None])

    def flaky_index(_chunks):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result

    monkeypatch.setattr(quality, "index_chunks", flaky_index)
    with pytest.raises(quality.QualityServiceError) as failure:
        quality.publish_latest(db, request.id, app_settings)
    assert failure.value.status_code == 503
    failed_entry = db.scalar(select(QualityKnowledgeEntry))
    failed_document = db.get(Document, failed_entry.document_id)
    original_document_id = failed_document.id
    assert failed_document.active is False
    assert failed_document.status == "indexing_failed"

    _, retried = quality.publish_latest(db, request.id, app_settings)
    assert retried.document_id == original_document_id
    assert db.get(Document, original_document_id).active is True


def test_published_entry_is_found_by_ordinary_retrieval_with_provenance(
    db, app_settings, monkeypatch
):
    trace, token = seed_escalation(db)
    request, _ = quality.create_quality_request(
        db,
        trace_id=trace.id,
        idempotency_key="idem-quality-retrieval",
        conversation_token=token,
        settings=app_settings,
    )
    approve_request(db, request)
    monkeypatch.setattr(quality, "index_chunks", lambda _chunks: None)
    _, entry = quality.publish_latest(db, request.id, app_settings)
    document = db.get(Document, entry.document_id)
    chunk_id = document.chunks[0].id

    class FakeQdrant:
        def query_points(self, **_kwargs):
            return SimpleNamespace(points=[SimpleNamespace(id=chunk_id)])

    class FakeOpenSearch:
        def search(self, **_kwargs):
            return {"hits": {"hits": [{"_id": str(chunk_id)}]}}

    factory = sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(search, "SessionLocal", factory)
    monkeypatch.setattr(search, "embed", lambda _texts: [[0.1, 0.2]])
    monkeypatch.setattr(search, "qdrant", lambda: FakeQdrant())
    monkeypatch.setattr(search, "opensearch", lambda: FakeOpenSearch())
    monkeypatch.setattr(search, "get_settings", lambda: app_settings)

    sources, _, _ = search.retrieve("Как оформить синтетическую заявку?")

    assert sources[0]["document_id"] == str(document.id)
    assert sources[0]["source_type"] == "quality_clarification"
    assert sources[0]["quality_request_number"] == request.public_number
    assert sources[0]["approval_number"] == entry.approval_number


def test_openapi_contains_quality_contract_and_no_recipient_field():
    from clip_agent.main import app

    schema = app.openapi()
    assert "/api/v1/quality-requests" in schema["paths"]
    create_schema = schema["components"]["schemas"]["QualityRequestCreate"]
    assert set(create_schema["properties"]) == {"trace_id"}


def test_additive_quality_schema_has_expected_tables_and_unique_constraints(db):
    inspector = inspect(db.bind)
    assert {
        "quality_requests",
        "quality_knowledge_entries",
        "quality_audit_events",
    }.issubset(inspector.get_table_names())
    request_uniques = {
        tuple(sorted(item["column_names"]))
        for item in inspector.get_unique_constraints("quality_requests")
    }
    request_uniques.update(
        tuple(sorted(item["column_names"]))
        for item in inspector.get_indexes("quality_requests")
        if item["unique"]
    )
    entry_uniques = {
        tuple(sorted(item["column_names"]))
        for item in inspector.get_unique_constraints("quality_knowledge_entries")
    }
    assert ("trace_id",) in request_uniques
    assert ("idempotency_key",) in request_uniques
    assert ("quality_request_id", "version") in entry_uniques
