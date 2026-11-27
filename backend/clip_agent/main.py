import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .agent import run_agent
from .analytics import FEEDBACK_RATINGS, aggregate_traces, classify_trace, trace_mode
from .answering import (
    GROUNDING_VALIDATION_SCHEMA,
    build_answer_prompt,
    build_citation_repair_prompt,
    build_grounding_validation_prompt,
    build_temporal_abstention,
    citation_ids_admissible,
    merge_llm_usage,
    parse_grounding_validation,
    requires_temporal_abstention,
)
from .chunking import chunk_text
from .config import get_settings
from .conversations import (
    CONVERSATION_TOKEN_HEADER,
    ConversationAccessError,
    add_turn,
    append_clarification,
    begin_new_question,
    bounded_reference_context,
    clarification_answers,
    create_conversation,
    is_referential_question,
    lock_conversation,
    resolved_query,
    serialize_conversation,
)
from .db import (
    Chunk,
    Conversation,
    ConversationTurn,
    Document,
    QualityKnowledgeEntry,
    QualityRequest,
    SearchTrace,
    SessionLocal,
    TraceFeedback,
    init_db,
    utcnow,
)
from .extract import extract_text
from .guardrails import assess_clarification_answer, assess_input
from .load_control import RequestGate, slot_dependency
from .llm import (
    LlmProviderError,
    LlmSelectionError,
    LlmSession,
    create_llm_session,
    llm_capabilities,
    openai_is_configured,
    resolve_llm_selection,
)
from .search import index_chunks, opensearch, qdrant, retrieve
from .quality import (
    QUALITY_APPROVAL_HEADER,
    QualityServiceError,
    approve_draft,
    create_quality_request,
    publish_latest,
    reject_request,
    require_approval_token,
    save_draft,
    send_notification,
    serialize_request,
)

settings = get_settings()
llm_gate = RequestGate(
    settings.demo_max_concurrent_llm,
    enabled=settings.public_demo_enabled,
    message="Сервис занят обработкой другого AI-запроса. Повторите попытку позже.",
)
ingest_gate = RequestGate(
    settings.demo_max_concurrent_ingest,
    enabled=settings.public_demo_enabled,
    message="Сервис занят индексацией другого документа. Повторите попытку позже.",
)
app = FastAPI(title="AI-Agent Clip", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", CONVERSATION_TOKEN_HEADER],
)


def get_db():
    with SessionLocal() as session:
        yield session


def acquire_llm_slot():
    yield from slot_dependency(llm_gate)


def acquire_ingest_slot():
    yield from slot_dependency(ingest_gate)


@app.on_event("startup")
def startup() -> None:
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    init_db()


class AskRequest(BaseModel):
    question: str = Field(max_length=4000)
    user_id: str = "demo-user"
    session_id: str = "demo-session"
    mode: Literal["baseline", "agent"] | None = None
    llm_provider: Literal["ollama", "openai"] | None = None
    llm_model: str | None = Field(default=None, min_length=1, max_length=200)
    conversation_id: UUID | None = None
    multi_query_enabled: bool | None = None


class FeedbackRequest(BaseModel):
    rating: Literal["helpful", "incomplete", "incorrect", "missing_documents"]
    user_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)


class QualityRequestCreate(BaseModel):
    trace_id: UUID


class QualityDraftRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=8000)
    scope: str = Field(min_length=1, max_length=2000)
    conditions: str = Field(default="", max_length=2000)
    expert_label: str = Field(min_length=1, max_length=200)


class QualityRejectRequest(BaseModel):
    expert_label: str = Field(min_length=1, max_length=200)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/capabilities")
def capabilities() -> dict:
    return {
        "default_mode": "agent" if settings.agent_enabled else "baseline",
        "agent_demo_override_enabled": settings.agent_demo_override_enabled,
        "llm": llm_capabilities(settings),
        "multi_query": {
            "available": settings.multi_query_enabled,
            "default_enabled": settings.multi_query_default_enabled,
            "max_variants": settings.multi_query_max_variants,
        },
    }


@app.get("/readyz")
def readyz(db: Session = Depends(get_db)) -> dict:
    checks: dict[str, Any] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "error"
    for name, check in (
        ("qdrant", lambda: qdrant().get_collections()),
        ("opensearch", lambda: opensearch().cluster.health()),
    ):
        try:
            check()
            checks[name] = "ok"
        except Exception:
            checks[name] = "error"
    provider_checks: dict[str, str] = {
        "openai": (
            "configured"
            if openai_is_configured(settings)
            else ("disabled" if not settings.openai_enabled else "not_configured")
        )
    }
    try:
        check_ollama_model()
        provider_checks["ollama"] = "ok"
    except Exception:
        provider_checks["ollama"] = "error"
    default_ready = (
        provider_checks["ollama"] == "ok"
        if settings.llm_default_provider == "ollama"
        else provider_checks["openai"] == "configured"
    )
    checks["llm_providers"] = provider_checks
    stores_ready = all(
        value == "ok" for key, value in checks.items() if key != "llm_providers"
    )
    if not stores_ready or not default_ready:
        raise HTTPException(503, detail=checks)
    return {"status": "ready", "checks": checks}


def check_ollama_model() -> None:
    response = httpx.get(f"{settings.ollama_url}/api/tags", timeout=5)
    response.raise_for_status()
    installed = {model["name"] for model in response.json().get("models", [])}
    if settings.ollama_model not in installed:
        raise RuntimeError(
            f"model {settings.ollama_model!r} is not installed; installed: {sorted(installed)}"
        )


@app.post("/api/v1/documents", status_code=201)
def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    db: Session = Depends(get_db),
    _slot: None = Depends(acquire_ingest_slot),
) -> dict:
    max_upload_bytes = settings.demo_max_upload_mb * 1024 * 1024
    data = file.file.read(max_upload_bytes + 1)
    if not data:
        raise HTTPException(400, "Пустой файл")
    if len(data) > max_upload_bytes:
        raise HTTPException(413, f"Размер файла больше {settings.demo_max_upload_mb} MB")
    document_id = uuid4()
    suffix = Path(file.filename or "source.txt").suffix.lower()
    storage_key = Path("documents") / str(document_id) / f"source{suffix}"
    target = settings.storage_root / storage_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    document = Document(
        id=document_id,
        title=title or Path(file.filename or "Документ").stem,
        filename=file.filename or "source.txt",
        storage_key=storage_key.as_posix(),
        status="indexing",
    )
    try:
        extracted = extract_text(document.filename, data)
        for ordinal, item in enumerate(chunk_text(extracted)):
            document.chunks.append(
                Chunk(
                    ordinal=ordinal,
                    page=item.page,
                    parent_text=item.parent_text,
                    child_text=item.child_text,
                    child_start=item.child_start,
                    child_end=item.child_end,
                )
            )
        db.add(document)
        db.commit()
        db.refresh(document)
        index_chunks(document.chunks)
        document.status = "ready"
        db.commit()
    except Exception as exc:
        db.rollback()
        failed = db.get(Document, document_id)
        if failed:
            failed.status = "failed"
            db.commit()
        raise HTTPException(500, detail="Индексация не выполнена") from exc
    return serialize_document(document)


def serialize_document(document: Document) -> dict:
    return {
        "id": str(document.id),
        "title": document.title,
        "filename": document.filename,
        "active": document.active,
        "status": document.status,
        "created_at": document.created_at.isoformat(),
        "chunks": len(document.chunks),
    }


@app.get("/api/v1/documents")
def list_documents(db: Session = Depends(get_db)) -> list[dict]:
    documents = db.scalars(select(Document).order_by(Document.created_at.desc())).all()
    return [serialize_document(document) for document in documents]


@app.post("/api/v1/documents/{document_id}/deactivate")
def deactivate(document_id: UUID, db: Session = Depends(get_db)) -> dict:
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "Документ не найден")
    document.active = False
    db.commit()
    return serialize_document(document)


@app.post("/api/v1/documents/{document_id}/restore")
def restore(document_id: UUID, db: Session = Depends(get_db)) -> dict:
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "Документ не найден")
    document.active = True
    db.commit()
    return serialize_document(document)


def _quality_http_error(exc: QualityServiceError) -> HTTPException:
    return HTTPException(exc.status_code, detail=str(exc))


@app.post("/api/v1/quality-requests", status_code=201)
def create_quality_escalation(
    payload: QualityRequestCreate,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
    conversation_token: Annotated[
        str | None, Header(alias=CONVERSATION_TOKEN_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        request, created = create_quality_request(
            db,
            trace_id=payload.trace_id,
            idempotency_key=idempotency_key,
            conversation_token=conversation_token,
            settings=settings,
        )
    except QualityServiceError as exc:
        raise _quality_http_error(exc) from exc
    result = serialize_request(db, request, detail=True)
    result["created"] = created
    return result


def _require_quality_role(token: str | None) -> None:
    try:
        require_approval_token(token, settings)
    except QualityServiceError as exc:
        raise _quality_http_error(exc) from exc


@app.get("/api/v1/quality-requests")
def list_quality_requests(
    approval_token: Annotated[
        str | None, Header(alias=QUALITY_APPROVAL_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    _require_quality_role(approval_token)
    rows = db.scalars(select(QualityRequest).order_by(QualityRequest.created_at.desc())).all()
    return [serialize_request(db, row) for row in rows]


@app.get("/api/v1/quality-requests/{request_id}")
def quality_request_detail(
    request_id: UUID,
    approval_token: Annotated[
        str | None, Header(alias=QUALITY_APPROVAL_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> dict:
    _require_quality_role(approval_token)
    request = db.get(QualityRequest, request_id)
    if request is None:
        raise HTTPException(404, "Обращение не найдено.")
    return serialize_request(db, request, detail=True)


@app.post("/api/v1/quality-requests/{request_id}/draft")
def update_quality_draft(
    request_id: UUID,
    payload: QualityDraftRequest,
    approval_token: Annotated[
        str | None, Header(alias=QUALITY_APPROVAL_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> dict:
    _require_quality_role(approval_token)
    request = db.get(QualityRequest, request_id)
    if request is None:
        raise HTTPException(404, "Обращение не найдено.")
    try:
        request = save_draft(
            db,
            request,
            title=payload.title.strip(),
            answer=payload.answer.strip(),
            scope=payload.scope.strip(),
            conditions=payload.conditions.strip(),
            expert_label=payload.expert_label.strip(),
        )
    except QualityServiceError as exc:
        raise _quality_http_error(exc) from exc
    return serialize_request(db, request, detail=True)


@app.post("/api/v1/quality-requests/{request_id}/approve")
def approve_quality_draft(
    request_id: UUID,
    approval_token: Annotated[
        str | None, Header(alias=QUALITY_APPROVAL_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> dict:
    _require_quality_role(approval_token)
    try:
        request, _entry = approve_draft(db, request_id)
    except QualityServiceError as exc:
        raise _quality_http_error(exc) from exc
    return serialize_request(db, request, detail=True)


@app.post("/api/v1/quality-requests/{request_id}/reject")
def reject_quality_request(
    request_id: UUID,
    payload: QualityRejectRequest,
    approval_token: Annotated[
        str | None, Header(alias=QUALITY_APPROVAL_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> dict:
    _require_quality_role(approval_token)
    request = db.get(QualityRequest, request_id)
    if request is None:
        raise HTTPException(404, "Обращение не найдено.")
    try:
        request = reject_request(db, request, payload.expert_label.strip())
    except QualityServiceError as exc:
        raise _quality_http_error(exc) from exc
    return serialize_request(db, request, detail=True)


@app.post("/api/v1/quality-requests/{request_id}/publish")
def publish_quality_knowledge(
    request_id: UUID,
    approval_token: Annotated[
        str | None, Header(alias=QUALITY_APPROVAL_HEADER)
    ] = None,
    db: Session = Depends(get_db),
    _slot: None = Depends(acquire_ingest_slot),
) -> dict:
    _require_quality_role(approval_token)
    try:
        request, _entry = publish_latest(db, request_id, settings)
    except QualityServiceError as exc:
        raise _quality_http_error(exc) from exc
    return serialize_request(db, request, detail=True)


@app.post("/api/v1/quality-requests/{request_id}/retry-email")
def retry_quality_email(
    request_id: UUID,
    approval_token: Annotated[
        str | None, Header(alias=QUALITY_APPROVAL_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> dict:
    _require_quality_role(approval_token)
    request = db.get(QualityRequest, request_id)
    if request is None:
        raise HTTPException(404, "Обращение не найдено.")
    if request.email_status != "failed":
        raise HTTPException(
            409,
            "Повтор разрешён только после явной ошибки; delivery_unknown не отправляется повторно.",
        )
    send_notification(db, request, settings)
    return serialize_request(db, request, detail=True)


@app.post("/api/v1/ask")
def ask(
    payload: AskRequest,
    db: Session = Depends(get_db),
    _slot: None = Depends(acquire_llm_slot),
    response: Response = None,
    conversation_token: Annotated[
        str | None, Header(alias=CONVERSATION_TOKEN_HEADER)
    ] = None,
) -> dict:
    if payload.mode is not None and not settings.agent_demo_override_enabled:
        raise HTTPException(403, "Request-level demo mode is disabled")
    try:
        selection = resolve_llm_selection(
            payload.llm_provider, payload.llm_model, settings=settings
        )
    except LlmSelectionError as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc
    llm = create_llm_session(selection, settings=settings)
    use_agent = settings.agent_enabled
    if payload.mode is not None:
        use_agent = payload.mode == "agent"
    if payload.multi_query_enabled is True and not settings.multi_query_enabled:
        raise HTTPException(409, "Multi-Query отключён конфигурацией сервера.")
    if payload.multi_query_enabled is True and not use_agent:
        raise HTTPException(409, "Multi-Query доступен только в режиме AI-Агента.")
    multi_query_requested = use_agent and settings.multi_query_enabled and (
        payload.multi_query_enabled
        if payload.multi_query_enabled is not None
        else settings.multi_query_default_enabled
    )
    multi_query_trace_defaults = {
        "multi_query_requested": multi_query_requested,
        "multi_query_effective": False,
        "multi_query_status": "not_run" if multi_query_requested else "disabled",
        "multi_query_queries": [],
        "multi_query_generated_count": 0,
        "multi_query_executed_count": 0,
        "multi_query_search_calls": 0,
        "multi_query_unique_sources": 0,
        "multi_query_deduplicated_count": 0,
        "multi_query_latency_ms": 0.0,
        "multi_query_fallback_reason": None,
    }
    if payload.conversation_id is not None and not use_agent:
        raise HTTPException(
            409,
            "Существующий диалог можно продолжить только в режиме AI-Агента.",
        )

    started = time.perf_counter()
    conversation: Conversation | None = None
    issued_token: str | None = None
    user_turn: ConversationTurn | None = None
    effective_question = payload.question
    follow_up = payload.conversation_id is not None
    clarification_follow_up = False
    clarification_assessment = None
    reference_context: str | None = None
    unresolved_reference = False

    if follow_up:
        if not isinstance(db, Session):
            raise HTTPException(409, "Conversation persistence requires a database session")
        try:
            conversation = lock_conversation(
                db,
                payload.conversation_id,
                conversation_token,
                allowed_statuses=("awaiting_clarification", "completed"),
            )
        except ConversationAccessError as exc:
            raise HTTPException(exc.status_code, detail=str(exc)) from exc
        if len(payload.question) > settings.conversation_max_message_chars:
            raise HTTPException(422, "Сообщение превышает допустимую длину.")
        clarification_follow_up = conversation.status == "awaiting_clarification"
        if clarification_follow_up:
            clarification_assessment = assess_clarification_answer(
                payload.question,
                original_question=conversation.original_question,
                clarification_question=conversation.pending_clarification or "",
            )
            if clarification_assessment.likely_new_question:
                raise HTTPException(
                    409,
                    "Сначала ответьте на уточнение или начните новый диалог.",
                )
        elif is_referential_question(payload.question):
            reference_context = bounded_reference_context(
                db,
                conversation,
                max_context_chars=settings.conversation_max_context_chars,
            )
            unresolved_reference = reference_context is None

    trace = SearchTrace(
        question=payload.question, user_id=payload.user_id, session_id=payload.session_id
    )
    db.add(trace)
    if isinstance(db, Session):
        db.flush()
    else:
        db.commit()
    mode = "langgraph_agent" if use_agent else "baseline"
    input_assessment = None if clarification_follow_up else assess_input(payload.question)
    if (
        input_assessment is not None
        and not input_assessment.allowed
        and conversation is None
    ):
        return _finish_controlled(
            db,
            trace,
            started,
            mode=mode,
            outcome=input_assessment.outcome or "invalid_input",
            reason_code=input_assessment.reason_code or "invalid_input",
            answer=input_assessment.message or "Запрос не может быть обработан.",
            agent_enabled=use_agent,
            details={**multi_query_trace_defaults, "input_guard": "rejected"},
            llm=llm,
        )

    if use_agent and isinstance(db, Session):
        if conversation is None:
            if len(payload.question) > settings.conversation_max_message_chars:
                raise HTTPException(422, "Сообщение превышает допустимую длину.")
            conversation, issued_token = create_conversation(
                db,
                original_question=payload.question,
                owner_id=payload.session_id,
                settings=settings,
            )
            user_turn = add_turn(
                db,
                conversation,
                role="user",
                kind="original_question",
                content=payload.question,
                trace_id=trace.id,
                llm_provider=selection.provider,
                llm_model=selection.model,
            )
        elif clarification_follow_up:
            user_turn = add_turn(
                db,
                conversation,
                role="user",
                kind=(
                    "clarification_answer"
                    if clarification_assessment and clarification_assessment.allowed
                    else "rejected_clarification"
                ),
                content=payload.question,
                trace_id=trace.id,
                llm_provider=selection.provider,
                llm_model=selection.model,
            )
            if clarification_assessment and clarification_assessment.allowed:
                conversation.user_answer_count += 1
                append_clarification(
                    conversation,
                    payload.question,
                    max_context_chars=settings.conversation_max_context_chars,
                )
                effective_question = resolved_query(conversation)
                conversation.status = "processing"
                conversation.version += 1
                conversation.updated_at = utcnow()
        else:
            begin_new_question(conversation, payload.question)
            user_turn = add_turn(
                db,
                conversation,
                role="user",
                kind="new_question",
                content=payload.question,
                trace_id=trace.id,
                llm_provider=selection.provider,
                llm_model=selection.model,
            )
            if reference_context is not None:
                effective_question = (
                    f"Текущий вопрос: {payload.question}\n"
                    "Ограниченный контекст предыдущего обмена (только для разрешения "
                    "ссылки, не доказательство корпоративных фактов):\n"
                    f"{reference_context}\n"
                    "Если объект ссылки нельзя установить однозначно, запроси уточнение. "
                    "Любой фактический ответ подтверждай только новым поиском по документам."
                )
        db.commit()

    conversation_details = {
        **_conversation_trace_details(conversation, user_turn, effective_question),
        **multi_query_trace_defaults,
    }
    if input_assessment is not None and not input_assessment.allowed:
        result = _finish_controlled(
            db,
            trace,
            started,
            mode=mode,
            outcome=input_assessment.outcome or "invalid_input",
            reason_code=input_assessment.reason_code or "invalid_input",
            answer=input_assessment.message or "Запрос не может быть обработан.",
            agent_enabled=True,
            details={**conversation_details, "input_guard": "rejected"},
            llm=llm,
        )
        return _finalize_conversation_result(
            db,
            conversation,
            trace,
            result,
            selection.provider,
            selection.model,
            response=response,
            issued_token=issued_token,
        )
    if unresolved_reference:
        result = _finish_controlled(
            db,
            trace,
            started,
            mode=mode,
            outcome="clarification_required",
            reason_code="unresolved_conversation_reference",
            answer=(
                "Уточните, пожалуйста, к какому документу, роли или объекту относится "
                "ваш вопрос?"
            ),
            agent_enabled=True,
            details={**conversation_details, "reference_context": "ambiguous"},
            llm=llm,
        )
        return _finalize_conversation_result(
            db,
            conversation,
            trace,
            result,
            selection.provider,
            selection.model,
            response=response,
            issued_token=issued_token,
        )
    if clarification_assessment and not clarification_assessment.allowed:
        result = _finish_controlled(
            db,
            trace,
            started,
            mode=mode,
            outcome="invalid_input",
            reason_code=clarification_assessment.reason_code or "invalid_clarification",
            answer=clarification_assessment.message or "Ответ на уточнение не принят.",
            agent_enabled=True,
            details={**conversation_details, "input_guard": "clarification_rejected"},
            llm=llm,
        )
        return _finalize_conversation_result(
            db,
            conversation,
            trace,
            result,
            selection.provider,
            selection.model,
            response=response,
            issued_token=issued_token,
            preserve_awaiting=True,
        )
    if clarification_assessment and clarification_assessment.user_does_not_know:
        result = _finish_controlled(
            db,
            trace,
            started,
            mode=mode,
            outcome="quality_review_required",
            reason_code="clarification_unknown",
            answer=(
                "Без запрошенного условия нельзя однозначно выбрать применимый регламент. "
                "Предлагаю обратиться в службу качества."
            ),
            agent_enabled=True,
            details=conversation_details,
            llm=llm,
        )
        return _finalize_conversation_result(
            db,
            conversation,
            trace,
            result,
            selection.provider,
            selection.model,
            response=response,
            issued_token=issued_token,
        )
    if (
        conversation is not None
        and conversation.user_answer_count > settings.conversation_max_user_answers
    ):
        result = _finish_controlled(
            db,
            trace,
            started,
            mode=mode,
            outcome="quality_review_required",
            reason_code="clarification_answer_limit",
            answer=(
                "Не удалось устранить неоднозначность в пределах допустимого числа ответов. "
                "Предлагаю обратиться в службу качества."
            ),
            agent_enabled=True,
            details=conversation_details,
            llm=llm,
        )
        return _finalize_conversation_result(
            db,
            conversation,
            trace,
            result,
            selection.provider,
            selection.model,
            response=response,
            issued_token=issued_token,
        )

    if use_agent:
        try:
            result = run_agent(
                effective_question,
                payload.user_id,
                payload.session_id,
                str(trace.id),
                llm=llm,
                multi_query_enabled=multi_query_requested,
            )
        except Exception as exc:
            controlled = _finish_controlled(
                db,
                trace,
                started,
                mode=mode,
                outcome="technical_error",
                reason_code="agent_runtime_error",
                answer=(
                    "Не удалось завершить обработку в режиме AI-Агента. "
                    "Неподтверждённый ответ не опубликован. Пожалуйста, повторите запрос позже."
                ),
                agent_enabled=True,
                details={
                    **conversation_details,
                    "error": str(exc),
                    "fallback_attempted": False,
                },
                llm=llm,
            )
            return _finalize_conversation_result(
                db,
                conversation,
                trace,
                controlled,
                selection.provider,
                selection.model,
                response=response,
                issued_token=issued_token,
                preserve_awaiting=clarification_follow_up,
            )
        latency_ms = round((time.perf_counter() - started) * 1000)
        sources = result["sources"]
        retrieval_trace = result["retrieval"]
        details = result["trace_details"]
        validation = details.get("validation", {})
        outcome = details.get("outcome", "technical_error")
        reason_code = details.get("reason_code") or details.get("refusal_reason")
        if outcome == "technical_error" and llm.last_error_category:
            reason_code = f"llm_{llm.last_error_category}"
            result["answer"] = (
                "Выбранный языковой провайдер не смог завершить запрос. "
                "Автоматическое переключение модели не выполнялось. Повторите запрос позже."
            )
        trace.answer = result["answer"]
        trace.retrieved_count = result["retrieved_count"]
        trace.selected_count = len(sources)
        trace.prompt_tokens = llm.input_tokens if llm.calls else None
        trace.completion_tokens = llm.output_tokens if llm.calls else None
        trace.latency_ms = latency_ms
        trace.status = "failed" if outcome == "technical_error" else "ok"
        trace.details = {
            **details,
            **conversation_details,
            **llm.summary(),
            "outcome": outcome,
            "reason_code": reason_code,
        }
        db.commit()
        api_result = {
            "answer": result["answer"],
            "sources": sources,
            "retrieval": _redact_internal_errors(retrieval_trace),
            "trace_id": str(trace.id),
            "outcome": outcome,
            "reason_code": reason_code,
            "llm_provider": selection.provider,
            "llm_model": selection.model,
            "metrics": {
                "latency_ms": latency_ms,
                "prompt_tokens": trace.prompt_tokens,
                "completion_tokens": trace.completion_tokens,
                "retrieved_count": result["retrieved_count"],
                "selected_count": len(sources),
                "done_reason": None,
                "citations_valid": validation.get("citations_valid", False),
                "empty_answer": not bool(result["answer"]),
                "citation_repair_attempted": validation.get("citation_repair_attempted", False),
                "temporal_guard_applied": reason_code == "temporal_evidence_gap",
                "rerank_status": retrieval_trace["status"],
                "rerank_latency_ms": retrieval_trace["latency_ms"],
                "rerank_fallback_reason": (
                    "reranker_fallback" if retrieval_trace["fallback_reason"] else None
                ),
                "agent_enabled": True,
                "agent_search_calls": details.get("search_calls", 0),
                "agent_llm_calls": details.get("llm_calls", 0),
                "llm_calls": llm.calls,
                "agent_steps": details.get("step_count", 0),
                "agent_retries": details.get("retries", 0),
                "llm_provider": selection.provider,
                "llm_model": selection.model,
                "provider_latency_ms": llm.summary()["provider_latency_ms"],
                "provider_error_category": llm.last_error_category,
                "multi_query_requested": details.get("multi_query_requested", False),
                "multi_query_effective": details.get("multi_query_effective", False),
                "multi_query_status": details.get("multi_query_status", "disabled"),
                "multi_query_queries": details.get("multi_query_queries", []),
                "multi_query_generated_count": details.get(
                    "multi_query_generated_count", 0
                ),
                "multi_query_executed_count": details.get(
                    "multi_query_executed_count", 0
                ),
                "multi_query_search_calls": details.get(
                    "multi_query_search_calls", 0
                ),
                "multi_query_unique_sources": details.get(
                    "multi_query_unique_sources", 0
                ),
                "multi_query_deduplicated_count": details.get(
                    "multi_query_deduplicated_count", 0
                ),
                "multi_query_latency_ms": details.get(
                    "multi_query_latency_ms", 0.0
                ),
                "multi_query_fallback_reason": details.get(
                    "multi_query_fallback_reason"
                ),
            },
        }
        return _finalize_conversation_result(
            db,
            conversation,
            trace,
            api_result,
            selection.provider,
            selection.model,
            response=response,
            issued_token=issued_token,
            preserve_awaiting=clarification_follow_up and outcome == "technical_error",
        )

    return _run_baseline(payload, db, trace, started, llm)


def _conversation_trace_details(
    conversation: Conversation | None,
    turn: ConversationTurn | None,
    resolved_question: str,
) -> dict[str, Any]:
    if conversation is None:
        return {}
    return {
        "conversation_id": str(conversation.id),
        "conversation_turn": turn.ordinal if turn else None,
        "original_question": conversation.original_question,
        "resolved_query": resolved_question,
        "clarification_answers": clarification_answers(conversation),
        "clarification_count": conversation.clarification_count,
        "clarification_answer_count": conversation.user_answer_count,
    }


def _finalize_conversation_result(
    db: Session,
    conversation: Conversation | None,
    trace: SearchTrace,
    result: dict[str, Any],
    llm_provider: str,
    llm_model: str,
    *,
    response: Response | None,
    issued_token: str | None,
    preserve_awaiting: bool = False,
) -> dict[str, Any]:
    if conversation is None or not isinstance(db, Session):
        return result

    outcome = str(result.get("outcome") or "technical_error")
    if outcome == "clarification_required" and (
        conversation.clarification_count >= settings.conversation_max_clarifications
        or conversation.user_answer_count >= settings.conversation_max_user_answers
    ):
        outcome = "quality_review_required"
        result["outcome"] = outcome
        result["reason_code"] = "clarification_limit_reached"
        result["answer"] = (
            "Не удалось устранить неоднозначность в пределах допустимого числа уточнений. "
            "Предлагаю обратиться в службу качества."
        )
        if isinstance(result.get("metrics"), dict):
            result["metrics"]["citations_valid"] = False

    if preserve_awaiting:
        conversation.status = "awaiting_clarification"
        kind = "clarification_rejected" if outcome == "invalid_input" else "technical_error"
    elif outcome == "clarification_required":
        conversation.status = "awaiting_clarification"
        conversation.pending_clarification = str(result["answer"])
        conversation.clarification_count += 1
        kind = "clarification_question"
    else:
        conversation.status = "completed"
        conversation.pending_clarification = None
        kind = "final_answer"
    conversation.version += 1
    conversation.updated_at = utcnow()
    assistant_turn = add_turn(
        db,
        conversation,
        role="assistant",
        kind=kind,
        content=str(result.get("answer") or ""),
        trace_id=trace.id,
        outcome=outcome,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )
    trace_details = trace.details if isinstance(trace.details, dict) else {}
    trace.details = {
        **trace_details,
        "conversation_id": str(conversation.id),
        "conversation_turn": assistant_turn.ordinal,
        "conversation_status": conversation.status,
        "outcome": outcome,
        "reason_code": result.get("reason_code"),
    }
    db.commit()
    result.update(
        {
            "conversation_id": str(conversation.id),
            "turn_id": str(assistant_turn.id),
            "conversation_status": conversation.status,
            "awaiting_clarification": conversation.status == "awaiting_clarification",
            "expires_at": conversation.expires_at.isoformat(),
        }
    )
    if issued_token and response is not None:
        response.headers[CONVERSATION_TOKEN_HEADER] = issued_token
    return result


@app.get("/api/v1/conversations/{conversation_id}")
def conversation_history(
    conversation_id: UUID,
    conversation_token: Annotated[
        str | None, Header(alias=CONVERSATION_TOKEN_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        conversation = lock_conversation(db, conversation_id, conversation_token)
    except ConversationAccessError as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc
    turns = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.conversation_id == conversation.id)
        .order_by(ConversationTurn.ordinal)
    ).all()
    serialized_turns = []
    for turn in turns:
        sources: list[dict[str, Any]] = []
        turn_metrics: dict[str, Any] | None = None
        if turn.trace_id is not None:
            trace = db.get(SearchTrace, turn.trace_id)
            if trace is not None:
                sources = _trace_sources(db, trace)
                details = trace.details if isinstance(trace.details, dict) else {}
                turn_metrics = {
                    "latency_ms": trace.latency_ms,
                    "prompt_tokens": trace.prompt_tokens,
                    "completion_tokens": trace.completion_tokens,
                    "multi_query_requested": details.get(
                        "multi_query_requested", False
                    ),
                    "multi_query_effective": details.get(
                        "multi_query_effective", False
                    ),
                    "multi_query_status": details.get(
                        "multi_query_status", "disabled"
                    ),
                    "multi_query_queries": details.get("multi_query_queries", []),
                    "multi_query_executed_count": details.get(
                        "multi_query_executed_count", 0
                    ),
                    "multi_query_search_calls": details.get(
                        "multi_query_search_calls", 0
                    ),
                    "multi_query_unique_sources": details.get(
                        "multi_query_unique_sources", 0
                    ),
                    "multi_query_deduplicated_count": details.get(
                        "multi_query_deduplicated_count", 0
                    ),
                    "multi_query_latency_ms": details.get(
                        "multi_query_latency_ms", 0.0
                    ),
                    "multi_query_fallback_reason": details.get(
                        "multi_query_fallback_reason"
                    ),
                }
        serialized_turns.append(
            {
                "id": str(turn.id),
                "ordinal": turn.ordinal,
                "role": turn.role,
                "kind": turn.kind,
                "content": turn.content,
                "trace_id": str(turn.trace_id) if turn.trace_id else None,
                "outcome": turn.outcome,
                "llm_provider": turn.llm_provider,
                "llm_model": turn.llm_model,
                "sources": sources if turn.role == "assistant" else [],
                "metrics": turn_metrics if turn.role == "assistant" else None,
                "created_at": turn.created_at.isoformat(),
            }
        )
    return {**serialize_conversation(conversation), "turns": serialized_turns}


@app.post("/api/v1/conversations/{conversation_id}/close")
def close_conversation(
    conversation_id: UUID,
    conversation_token: Annotated[
        str | None, Header(alias=CONVERSATION_TOKEN_HEADER)
    ] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        conversation = lock_conversation(db, conversation_id, conversation_token)
    except ConversationAccessError as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc
    conversation.status = "closed"
    conversation.pending_clarification = None
    conversation.version += 1
    conversation.updated_at = utcnow()
    db.commit()
    return serialize_conversation(conversation)


def _finish_controlled(
    db: Session,
    trace: SearchTrace,
    started: float,
    *,
    mode: str,
    outcome: str,
    reason_code: str,
    answer: str,
    agent_enabled: bool,
    sources: list[dict] | None = None,
    retrieved_count: int = 0,
    retrieval: dict | None = None,
    details: dict | None = None,
    usage: dict | None = None,
    llm: LlmSession | None = None,
) -> dict:
    sources = sources or []
    usage = usage or {}
    retrieval = retrieval or {
        "enabled": False,
        "status": "not_run",
        "latency_ms": 0.0,
        "fallback_reason": None,
    }
    latency_ms = round((time.perf_counter() - started) * 1000)
    trace.answer = answer
    trace.retrieved_count = retrieved_count
    trace.selected_count = len(sources)
    llm_summary = llm.summary() if llm else {}
    trace.prompt_tokens = (
        llm_summary.get("input_tokens") if llm_summary.get("llm_calls") else None
    )
    trace.completion_tokens = (
        llm_summary.get("output_tokens") if llm_summary.get("llm_calls") else None
    )
    trace.latency_ms = latency_ms
    trace.status = "failed" if outcome == "technical_error" else "ok"
    trace.details = {
        "mode": mode,
        "outcome": outcome,
        "reason_code": reason_code,
        "source_ids": [source.get("chunk_id") for source in sources],
        "llm_calls": 0,
        **(details or {}),
        **llm_summary,
    }
    db.commit()
    return {
        "answer": answer,
        "sources": sources,
        "retrieval": _redact_internal_errors(retrieval),
        "trace_id": str(trace.id),
        "outcome": outcome,
        "reason_code": reason_code,
        "llm_provider": llm_summary.get("llm_provider"),
        "llm_model": llm_summary.get("llm_model"),
        "metrics": {
            "latency_ms": latency_ms,
            "prompt_tokens": trace.prompt_tokens,
            "completion_tokens": trace.completion_tokens,
            "retrieved_count": retrieved_count,
            "selected_count": len(sources),
            "citations_valid": False,
            "empty_answer": False,
            "done_reason": usage.get("done_reason"),
            "citation_repair_attempted": bool(
                (details or {}).get("citation_repair_attempted", False)
            ),
            "temporal_guard_applied": reason_code == "temporal_evidence_gap",
            "rerank_status": retrieval.get("status"),
            "rerank_latency_ms": retrieval.get("latency_ms"),
            "rerank_fallback_reason": (
                "reranker_fallback" if retrieval.get("fallback_reason") else None
            ),
            "agent_enabled": agent_enabled,
            "agent_search_calls": 0,
            "agent_llm_calls": int((details or {}).get("llm_calls", 0)),
            "llm_calls": llm_summary.get("llm_calls", 0),
            "agent_steps": 0,
            "agent_retries": 0,
            "llm_provider": llm_summary.get("llm_provider"),
            "llm_model": llm_summary.get("llm_model"),
            "provider_latency_ms": llm_summary.get("provider_latency_ms"),
            "provider_error_category": llm_summary.get("provider_error_category"),
        },
    }


def _run_baseline(
    payload: AskRequest,
    db: Session,
    trace: SearchTrace,
    started: float,
    llm: LlmSession,
) -> dict:
    citation_repair_attempted = False
    citation_repair_error = None
    temporal_guard_applied = False
    llm_calls = 0
    try:
        sources, retrieved_count, retrieval_trace = retrieve(payload.question)
        if not sources:
            return _finish_controlled(
                db,
                trace,
                started,
                mode="baseline",
                outcome="quality_review_required",
                reason_code="no_evidence",
                answer=(
                    "В доступных регламентах не найдено достаточного подтверждения для "
                    "однозначного ответа. Предлагаю обратиться в службу качества."
                ),
                agent_enabled=False,
                retrieved_count=retrieved_count,
                retrieval=retrieval_trace,
                llm=llm,
            )
        elif requires_temporal_abstention(payload.question, sources):
            answer = build_temporal_abstention(sources)
            usage = {}
            citations_valid = citation_ids_admissible(answer, len(sources))
            temporal_guard_applied = True
            outcome = "quality_review_required"
            reason_code = "temporal_evidence_gap"
        else:
            prompt = build_answer_prompt(payload.question, sources)
            answer, usage = llm.generate(prompt)
            llm_calls += 1
            citations_valid = citation_ids_admissible(answer, len(sources))
            if not citations_valid:
                citation_repair_attempted = True
                try:
                    repaired_answer, repair_usage = llm.generate(
                        build_citation_repair_prompt(prompt, answer, len(sources))
                    )
                    llm_calls += 1
                    usage = merge_llm_usage(usage, repair_usage)
                    if repaired_answer:
                        answer = repaired_answer
                except LlmProviderError:
                    raise
                except (KeyError, TypeError, ValueError) as exc:
                    citation_repair_error = str(exc)
                citations_valid = citation_ids_admissible(answer, len(sources))
            if not citations_valid:
                return _finish_controlled(
                    db,
                    trace,
                    started,
                    mode="baseline",
                    outcome="quality_review_required",
                    reason_code="invalid_or_missing_citations",
                    answer=(
                        "По найденным источникам нельзя надёжно подтвердить ответ: проверка "
                        "цитат не пройдена. Предлагаю обратиться в службу качества."
                    ),
                    agent_enabled=False,
                    sources=sources,
                    retrieved_count=retrieved_count,
                    retrieval=retrieval_trace,
                    usage=usage,
                    details={
                        "citation_repair_attempted": citation_repair_attempted,
                        "citation_repair_error": citation_repair_error,
                        "llm_calls": llm_calls,
                    },
                    llm=llm,
                )

            validation_text, validation_usage = llm.generate(
                build_grounding_validation_prompt(payload.question, answer, sources),
                json_schema=GROUNDING_VALIDATION_SCHEMA,
                schema_name="grounding_validation",
            )
            llm_calls += 1
            usage = merge_llm_usage(usage, validation_usage)
            grounded, validation_failures = parse_grounding_validation(validation_text)
            if not grounded:
                return _finish_controlled(
                    db,
                    trace,
                    started,
                    mode="baseline",
                    outcome="quality_review_required",
                    reason_code="answer_validation_failed",
                    answer=(
                        "По найденным источникам нельзя надёжно подтвердить итоговый вывод. "
                        "Неподтверждённый ответ не опубликован. Предлагаю обратиться в службу качества."
                    ),
                    agent_enabled=False,
                    sources=sources,
                    retrieved_count=retrieved_count,
                    retrieval=retrieval_trace,
                    usage=usage,
                    details={"validation_failures": validation_failures, "llm_calls": llm_calls},
                    llm=llm,
                )
            outcome = "answered"
            reason_code = "grounded_answer"
        latency_ms = round((time.perf_counter() - started) * 1000)
        trace.answer = answer
        trace.retrieved_count = retrieved_count
        trace.selected_count = len(sources)
        trace.prompt_tokens = llm.input_tokens if llm.calls else None
        trace.completion_tokens = llm.output_tokens if llm.calls else None
        trace.latency_ms = latency_ms
        trace.status = "ok"
        trace.details = {
            "mode": "baseline",
            "outcome": outcome,
            "reason_code": reason_code,
            "source_ids": [source["chunk_id"] for source in sources],
            "done_reason": usage.get("done_reason"),
            "citations_valid": citations_valid if sources else False,
            "empty_answer": False,
            "citation_repair_attempted": citation_repair_attempted,
            "temporal_guard_applied": temporal_guard_applied,
            "citation_repair_error": citation_repair_error,
            "llm_calls": llm_calls,
            "reranking": retrieval_trace,
            **llm.summary(),
            **(
                {"ollama": ollama_metrics(usage)}
                if llm.selection.provider == "ollama"
                else {}
            ),
        }
        db.commit()
        return {
            "answer": answer,
            "sources": sources,
            "retrieval": _redact_internal_errors(retrieval_trace),
            "trace_id": str(trace.id),
            "outcome": outcome,
            "reason_code": reason_code,
            "llm_provider": llm.selection.provider,
            "llm_model": llm.selection.model,
            "metrics": {
                "latency_ms": latency_ms,
                "prompt_tokens": trace.prompt_tokens,
                "completion_tokens": trace.completion_tokens,
                "retrieved_count": retrieved_count,
                "selected_count": len(sources),
                "done_reason": usage.get("done_reason"),
                "citations_valid": citations_valid if sources else False,
                "empty_answer": False,
                "citation_repair_attempted": citation_repair_attempted,
                "temporal_guard_applied": temporal_guard_applied,
                "rerank_status": retrieval_trace["status"],
                "rerank_latency_ms": retrieval_trace["latency_ms"],
                "rerank_fallback_reason": (
                    "reranker_fallback" if retrieval_trace["fallback_reason"] else None
                ),
                "agent_enabled": False,
                "llm_calls": llm.calls,
                "llm_provider": llm.selection.provider,
                "llm_model": llm.selection.model,
                "provider_latency_ms": llm.summary()["provider_latency_ms"],
                "provider_error_category": llm.last_error_category,
                **(
                    ollama_metrics(usage)
                    if llm.selection.provider == "ollama"
                    else {}
                ),
            },
        }
    except LlmProviderError as exc:
        return _finish_controlled(
            db,
            trace,
            started,
            mode="baseline",
            outcome="technical_error",
            reason_code=f"llm_{exc.category}",
            answer=(
                "Выбранный языковой провайдер не смог завершить запрос. "
                "Автоматическое переключение модели не выполнялось. Повторите запрос позже."
            ),
            agent_enabled=False,
            details={"provider_error_category": exc.category, "llm_calls": llm.calls},
            llm=llm,
        )
    except Exception as exc:
        return _finish_controlled(
            db,
            trace,
            started,
            mode="baseline",
            outcome="technical_error",
            reason_code="baseline_runtime_error",
            answer=(
                "Не удалось завершить обработку запроса. Неподтверждённый ответ не "
                "опубликован. Пожалуйста, повторите запрос позже."
            ),
            agent_enabled=False,
            details={"error": str(exc), "llm_calls": llm_calls},
            llm=llm,
        )


def ollama_metrics(payload: dict) -> dict:
    result = {}
    for name in (
        "total_duration",
        "load_duration",
        "prompt_eval_duration",
        "eval_duration",
    ):
        value = payload.get(name)
        result[f"{name}_ms"] = round(value / 1_000_000, 3) if value is not None else None
    return result


def _trace_query(
    *,
    status: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
):
    statement = select(SearchTrace)
    if status:
        statement = statement.where(SearchTrace.status == status)
    if user_id:
        statement = statement.where(SearchTrace.user_id == user_id)
    if session_id:
        statement = statement.where(SearchTrace.session_id == session_id)
    if created_after:
        statement = statement.where(SearchTrace.created_at >= created_after)
    if created_before:
        statement = statement.where(SearchTrace.created_at <= created_before)
    return statement.order_by(SearchTrace.created_at.desc())


def _feedback_by_trace(db: Session, trace_ids: list[UUID]) -> dict[str, list[str]]:
    if not trace_ids:
        return {}
    rows = db.execute(
        select(TraceFeedback.trace_id, TraceFeedback.rating).where(
            TraceFeedback.trace_id.in_(trace_ids)
        )
    ).all()
    result: dict[str, list[str]] = {}
    for trace_id, rating in rows:
        result.setdefault(str(trace_id), []).append(rating)
    return result


def _redact_internal_errors(value):
    if isinstance(value, list):
        return [_redact_internal_errors(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"error", "fallback_reason", "citation_repair_error"}:
            result[key] = "recorded" if item else None
        elif key == "agent_fallback_reason":
            result[key] = "agent_runtime_error" if item else None
        else:
            result[key] = _redact_internal_errors(item)
    return result


def _serialize_feedback(row: TraceFeedback) -> dict:
    return {
        "id": str(row.id),
        "trace_id": str(row.trace_id),
        "rating": row.rating,
        "created_at": row.created_at.isoformat(),
    }


def _serialize_trace(
    row: SearchTrace,
    feedback: list[str] | None = None,
    *,
    include_answer: bool = False,
) -> dict:
    classification = classify_trace(row, feedback or [])
    result = {
        "id": str(row.id),
        "question": row.question,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "mode": trace_mode(row),
        "status": row.status,
        "technical_status": classification["technical_status"],
        "quality_status": classification["quality_status"],
        "outcome": classification["outcome"],
        "reason_code": row.details.get("reason_code") if isinstance(row.details, dict) else None,
        "categories": classification["categories"],
        "latency_ms": row.latency_ms,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "retrieved_count": row.retrieved_count,
        "selected_count": row.selected_count,
        "details": _redact_internal_errors(row.details or {}),
        "created_at": row.created_at.isoformat(),
    }
    if include_answer:
        result["answer"] = row.answer
    return result


def _trace_sources(db: Session, row: SearchTrace) -> list[dict]:
    details = row.details if isinstance(row.details, dict) else {}
    source_ids = []
    for value in details.get("source_ids", []):
        try:
            source_ids.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if not source_ids:
        return []
    records = db.execute(
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.id.in_(source_ids))
    ).all()
    by_id = {chunk.id: (chunk, document) for chunk, document in records}
    document_ids = {document.id for _, document in records}
    quality_rows = db.execute(
        select(QualityKnowledgeEntry, QualityRequest)
        .join(
            QualityRequest,
            QualityKnowledgeEntry.quality_request_id == QualityRequest.id,
        )
        .where(
            QualityKnowledgeEntry.document_id.in_(document_ids),
            QualityKnowledgeEntry.status == "published",
        )
    ).all() if document_ids else []
    quality_by_document = {
        entry.document_id: (entry, request) for entry, request in quality_rows
    }
    return [
        {
            "chunk_id": str(chunk.id),
            "document_id": str(document.id),
            "title": document.title,
            "filename": document.filename,
            "page": chunk.page,
            "parent_text": chunk.parent_text,
            "child_text": chunk.child_text,
            "child_start": chunk.child_start,
            "child_end": chunk.child_end,
            **(
                {
                    "source_type": "quality_clarification",
                    "quality_request_number": quality_by_document[document.id][1].public_number,
                    "quality_knowledge_entry_id": str(
                        quality_by_document[document.id][0].id
                    ),
                    "approval_number": quality_by_document[document.id][0].approval_number,
                    "approved_at": quality_by_document[document.id][0].approved_at.isoformat(),
                    "scope": quality_by_document[document.id][0].scope,
                }
                if document.id in quality_by_document
                else {"source_type": "corporate_document"}
            ),
        }
        for source_id in source_ids
        if source_id in by_id
        for chunk, document in [by_id[source_id]]
    ]


@app.get("/api/v1/admin/analytics")
def analytics(
    user_id: str | None = None,
    session_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict:
    total_documents = db.scalar(select(func.count()).select_from(Document)) or 0
    active_documents = (
        db.scalar(select(func.count()).select_from(Document).where(Document.active.is_(True))) or 0
    )
    rows = db.scalars(
        _trace_query(
            user_id=user_id,
            session_id=session_id,
            created_after=created_after,
            created_before=created_before,
        )
    ).all()
    feedback = _feedback_by_trace(db, [row.id for row in rows])
    quality_counts = {
        "quality_requests_created": db.scalar(
            select(func.count()).select_from(QualityRequest)
        )
        or 0,
        "quality_emails_sent": db.scalar(
            select(func.count())
            .select_from(QualityRequest)
            .where(QualityRequest.email_status == "sent")
        )
        or 0,
        "quality_email_failures": db.scalar(
            select(func.count())
            .select_from(QualityRequest)
            .where(QualityRequest.email_status.in_(["failed", "delivery_unknown"]))
        )
        or 0,
        "quality_requests_pending": db.scalar(
            select(func.count())
            .select_from(QualityRequest)
            .where(QualityRequest.status.in_(["new", "awaiting_expert", "draft"]))
        )
        or 0,
        "quality_knowledge_approved": db.scalar(
            select(func.count()).select_from(QualityKnowledgeEntry)
        )
        or 0,
        "quality_knowledge_published": db.scalar(
            select(func.count())
            .select_from(QualityKnowledgeEntry)
            .where(QualityKnowledgeEntry.status == "published")
        )
        or 0,
        "quality_requests_rejected": db.scalar(
            select(func.count())
            .select_from(QualityRequest)
            .where(QualityRequest.status == "rejected")
        )
        or 0,
    }
    return {
        "total_documents": total_documents,
        "active_documents": active_documents,
        **quality_counts,
        **aggregate_traces(rows, feedback),
    }


@app.get("/api/v1/admin/traces")
def traces(
    response: Response,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = None,
    mode: Literal["baseline", "agent"] | None = None,
    category: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    if category and category not in {
        "technical_error",
        "no_evidence",
        "evidence_gap",
        "justified_abstention",
        "citation_problem",
        "agent_fallback",
        "manual_review_required",
        "answer_produced",
    }:
        raise HTTPException(422, "Unknown trace category")
    rows = db.scalars(
        _trace_query(
            status=status,
            user_id=user_id,
            session_id=session_id,
            created_after=created_after,
            created_before=created_before,
        )
    ).all()
    feedback = _feedback_by_trace(db, [row.id for row in rows])
    if mode:
        rows = [row for row in rows if trace_mode(row) == mode]
    if category:
        rows = [
            row
            for row in rows
            if category in classify_trace(row, feedback.get(str(row.id), []))["categories"]
        ]
    response.headers["X-Total-Count"] = str(len(rows))
    return [
        _serialize_trace(row, feedback.get(str(row.id))) for row in rows[offset : offset + limit]
    ]


@app.get("/api/v1/admin/traces/{trace_id}")
def trace_detail(trace_id: UUID, db: Session = Depends(get_db)) -> dict:
    row = db.get(SearchTrace, trace_id)
    if row is None:
        raise HTTPException(404, "Trace not found")
    feedback_rows = db.scalars(
        select(TraceFeedback)
        .where(TraceFeedback.trace_id == trace_id)
        .order_by(TraceFeedback.created_at.desc())
    ).all()
    result = _serialize_trace(
        row, [item.rating for item in feedback_rows], include_answer=True
    )
    result["sources"] = _trace_sources(db, row)
    result["feedback"] = [_serialize_feedback(item) for item in feedback_rows]
    return result


@app.post("/api/v1/traces/{trace_id}/feedback", status_code=201)
def create_feedback(
    trace_id: UUID, payload: FeedbackRequest, db: Session = Depends(get_db)
) -> dict:
    if payload.rating not in FEEDBACK_RATINGS:
        raise HTTPException(422, "Unknown feedback rating")
    trace = db.get(SearchTrace, trace_id)
    if trace is None:
        raise HTTPException(404, "Trace not found")
    if trace.user_id != payload.user_id or trace.session_id != payload.session_id:
        raise HTTPException(404, "Trace not found for this user and session")
    feedback = TraceFeedback(
        trace_id=trace_id,
        user_id=payload.user_id,
        session_id=payload.session_id,
        rating=payload.rating,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return _serialize_feedback(feedback)
