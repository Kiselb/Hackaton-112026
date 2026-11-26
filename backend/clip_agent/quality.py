from __future__ import annotations

import hashlib
import hmac
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .chunking import chunk_text
from .config import Settings
from .conversations import is_expired, token_matches
from .db import (
    Chunk,
    Conversation,
    ConversationTurn,
    Document,
    QualityAuditEvent,
    QualityKnowledgeEntry,
    QualityRequest,
    SearchTrace,
    utcnow,
)
from .search import index_chunks


QUALITY_APPROVAL_HEADER = "X-Quality-Approval-Token"
ALLOWED_ESCALATION_OUTCOMES = {"quality_review_required", "conflict_detected"}


class QualityServiceError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def require_approval_token(token: str | None, settings: Settings) -> None:
    configured = settings.quality_approval_token
    if configured is None or not configured.get_secret_value():
        raise QualityServiceError(503, "Код роли службы качества не настроен.")
    if token is None or not hmac.compare_digest(token, configured.get_secret_value()):
        raise QualityServiceError(403, "Недостаточно прав для операции службы качества.")


def _safe_address(value: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise ValueError("invalid email configuration")
    _, address = parseaddr(value)
    if not address or "@" not in address:
        raise ValueError("invalid email configuration")
    return address


def build_notification_message(request: QualityRequest, settings: Settings) -> EmailMessage:
    sender = _safe_address(settings.quality_smtp_from)
    recipient = _safe_address(settings.quality_email_to)
    context = request.clarification_context if isinstance(request.clarification_context, dict) else {}
    answers = context.get("answers")
    clarification = "\n".join(f"- {value}" for value in answers if isinstance(value, str)) if isinstance(answers, list) else ""
    if not clarification:
        clarification = "Уточнения не предоставлены."
    body = (
        f"Обращение: {request.public_number}\n"
        f"Дата создания: {request.created_at.isoformat()}\n"
        f"Причина: {request.reason_code}\n\n"
        f"Исходный вопрос:\n{request.question}\n\n"
        f"Допустимые уточнения:\n{clarification}\n\n"
        "Откройте вкладку «Служба качества» в приложении и найдите обращение по номеру."
    )
    if len(body) > settings.quality_email_max_body_chars:
        body = body[: settings.quality_email_max_body_chars - 40] + "\n[Содержимое сокращено сервером]"
    message = EmailMessage()
    message["Subject"] = f"AI-Agent Clip: обращение {request.public_number}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)
    return message


def _send_message(message: EmailMessage, settings: Settings) -> None:
    if not settings.quality_smtp_host or "\r" in settings.quality_smtp_host or "\n" in settings.quality_smtp_host:
        raise ValueError("invalid smtp configuration")
    with smtplib.SMTP(
        settings.quality_smtp_host,
        settings.quality_smtp_port,
        timeout=settings.quality_smtp_timeout_seconds,
    ) as client:
        client.ehlo()
        if settings.quality_smtp_starttls:
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        password = settings.quality_smtp_password
        if settings.quality_smtp_username:
            if password is None:
                raise ValueError("invalid smtp configuration")
            client.login(settings.quality_smtp_username, password.get_secret_value())
        client.send_message(message)


def send_notification(db: Session, request: QualityRequest, settings: Settings) -> None:
    if not settings.quality_email_enabled:
        request.email_status = "not_requested"
        request.email_error_category = "email_disabled"
        db.commit()
        return
    request.email_status = "sending"
    request.email_attempted_at = utcnow()
    request.email_error_category = None
    db.commit()
    try:
        message = build_notification_message(request, settings)
        _send_message(message, settings)
    except (ValueError, smtplib.SMTPAuthenticationError):
        request.email_status = "failed"
        request.email_error_category = "configuration_or_authentication"
    except smtplib.SMTPRecipientsRefused:
        request.email_status = "failed"
        request.email_error_category = "recipient_rejected"
    except (smtplib.SMTPServerDisconnected, TimeoutError, OSError):
        request.email_status = "delivery_unknown"
        request.email_error_category = "transport_result_unknown"
    except smtplib.SMTPException:
        request.email_status = "failed"
        request.email_error_category = "smtp_rejected"
    else:
        request.email_status = "sent"
        request.email_sent_at = utcnow()
    request.updated_at = utcnow()
    db.commit()


def _audit(
    db: Session,
    request: QualityRequest,
    action: str,
    *,
    entry: QualityKnowledgeEntry | None = None,
    actor_label: str | None = None,
    authorization_method: str,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        QualityAuditEvent(
            quality_request_id=request.id,
            quality_knowledge_entry_id=entry.id if entry else None,
            action=action,
            actor_label=actor_label,
            authorization_method=authorization_method,
            details=details or {},
        )
    )


def create_quality_request(
    db: Session,
    *,
    trace_id: UUID,
    idempotency_key: str,
    conversation_token: str | None,
    settings: Settings,
) -> tuple[QualityRequest, bool]:
    existing = db.scalar(
        select(QualityRequest).where(QualityRequest.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.trace_id != trace_id:
            raise QualityServiceError(409, "Idempotency key уже использован для другого обращения.")
        existing_conversation = db.get(Conversation, existing.conversation_id)
        if (
            existing_conversation is None
            or is_expired(existing_conversation)
            or not token_matches(existing_conversation, conversation_token)
        ):
            raise QualityServiceError(404, "Диалог не найден.")
        return existing, False
    trace = db.get(SearchTrace, trace_id)
    if trace is None:
        raise QualityServiceError(404, "Основание обращения не найдено.")
    details = trace.details if isinstance(trace.details, dict) else {}
    outcome = details.get("outcome")
    if outcome not in ALLOWED_ESCALATION_OUTCOMES:
        raise QualityServiceError(409, "Этот результат не допускает обращение в службу качества.")
    raw_conversation_id = details.get("conversation_id")
    try:
        conversation_id = UUID(str(raw_conversation_id))
    except (TypeError, ValueError) as exc:
        raise QualityServiceError(
            409,
            "Legacy trace без защищённого диалога нельзя использовать для обращения.",
        ) from exc
    conversation = db.get(Conversation, conversation_id)
    if (
        conversation is None
        or is_expired(conversation)
        or not token_matches(conversation, conversation_token)
    ):
        raise QualityServiceError(404, "Диалог не найден.")
    owns_trace = db.scalar(
        select(func.count())
        .select_from(ConversationTurn)
        .where(
            ConversationTurn.conversation_id == conversation.id,
            ConversationTurn.trace_id == trace.id,
        )
    )
    if not owns_trace:
        raise QualityServiceError(404, "Диалог не найден.")
    since = utcnow() - timedelta(hours=1)
    recent = db.scalar(
        select(func.count()).select_from(QualityRequest).where(QualityRequest.created_at >= since)
    ) or 0
    if recent >= settings.quality_request_rate_limit_per_hour:
        raise QualityServiceError(429, "Лимит обращений временно исчерпан.")
    request_id = uuid4()
    request = QualityRequest(
        id=request_id,
        public_number=f"QA-{request_id.hex[:8].upper()}",
        trace_id=trace.id,
        conversation_id=conversation.id,
        idempotency_key=idempotency_key,
        question=conversation.original_question,
        reason_code=str(details.get("reason_code") or "evidence_not_confirmed")[:100],
        source_outcome=str(outcome),
        clarification_context={
            "answers": list((conversation.clarification_context or {}).get("answers", [])),
            "clarification_count": conversation.clarification_count,
        },
        status="awaiting_expert",
        email_status="queued" if settings.quality_email_enabled else "not_requested",
    )
    db.add(request)
    _audit(
        db,
        request,
        "request_created",
        authorization_method="conversation_ownership_token",
        details={"source_outcome": outcome},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.scalar(
            select(QualityRequest).where(
                (QualityRequest.idempotency_key == idempotency_key)
                | (QualityRequest.trace_id == trace_id)
            )
        )
        if concurrent is None:
            raise
        if concurrent.trace_id != trace_id:
            raise QualityServiceError(
                409, "Idempotency key уже использован для другого обращения."
            )
        return concurrent, False
    db.refresh(request)
    send_notification(db, request, settings)
    return request, True


def save_draft(
    db: Session,
    request: QualityRequest,
    *,
    title: str,
    answer: str,
    scope: str,
    conditions: str,
    expert_label: str,
) -> QualityRequest:
    if request.status == "rejected":
        raise QualityServiceError(409, "Отклонённое обращение нельзя изменить.")
    request.draft_title = title
    request.draft_answer = answer
    request.draft_scope = scope
    request.draft_conditions = conditions
    request.draft_expert_label = expert_label
    request.status = "draft"
    request.reviewed_at = request.reviewed_at or utcnow()
    request.updated_at = utcnow()
    _audit(
        db,
        request,
        "draft_saved",
        actor_label=expert_label,
        authorization_method="demo_quality_approval_token",
    )
    db.commit()
    return request


def approve_draft(db: Session, request_id: UUID) -> tuple[QualityRequest, QualityKnowledgeEntry]:
    request = db.scalar(
        select(QualityRequest).where(QualityRequest.id == request_id).with_for_update()
    )
    if request is None:
        raise QualityServiceError(404, "Обращение не найдено.")
    if request.status != "draft":
        raise QualityServiceError(409, "Сначала сохраните полный draft разъяснения.")
    required = (
        request.draft_title,
        request.draft_answer,
        request.draft_scope,
        request.draft_expert_label,
    )
    if not all(value and value.strip() for value in required):
        raise QualityServiceError(422, "Не заполнены обязательные поля разъяснения.")
    latest = db.scalar(
        select(func.max(QualityKnowledgeEntry.version)).where(
            QualityKnowledgeEntry.quality_request_id == request.id
        )
    ) or 0
    now = utcnow()
    canonical = "\n".join(
        [
            request.question,
            request.draft_title or "",
            request.draft_answer or "",
            request.draft_scope or "",
            request.draft_conditions or "",
        ]
    )
    entry_id = uuid4()
    entry = QualityKnowledgeEntry(
        id=entry_id,
        quality_request_id=request.id,
        version=latest + 1,
        approval_number=f"QK-{now:%Y%m%d}-{entry_id.hex[:8].upper()}",
        original_question=request.question,
        title=request.draft_title or "",
        approved_text=request.draft_answer or "",
        scope=request.draft_scope or "",
        conditions=request.draft_conditions or "",
        status="approved",
        approval_method="demo_quality_approval_token",
        expert_label=request.draft_expert_label or "unverified demonstrator",
        checksum=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        approved_at=now,
    )
    db.add(entry)
    request.status = "approved"
    request.approved_at = now
    request.updated_at = now
    _audit(
        db,
        request,
        "knowledge_approved",
        entry=entry,
        actor_label=entry.expert_label,
        authorization_method=entry.approval_method,
        details={"version": entry.version, "checksum": entry.checksum},
    )
    db.commit()
    db.refresh(entry)
    return request, entry


def reject_request(db: Session, request: QualityRequest, actor_label: str) -> QualityRequest:
    if request.status in {"approved", "published"}:
        raise QualityServiceError(409, "Утверждённую версию нельзя отклонить задним числом.")
    published_exists = db.scalar(
        select(func.count())
        .select_from(QualityKnowledgeEntry)
        .where(
            QualityKnowledgeEntry.quality_request_id == request.id,
            QualityKnowledgeEntry.status == "published",
        )
    )
    request.status = "published" if published_exists else "rejected"
    request.reviewed_at = request.reviewed_at or utcnow()
    request.updated_at = utcnow()
    _audit(
        db,
        request,
        "correction_rejected" if published_exists else "request_rejected",
        actor_label=actor_label,
        authorization_method="demo_quality_approval_token",
    )
    db.commit()
    return request


def _entry_text(request: QualityRequest, entry: QualityKnowledgeEntry) -> str:
    conditions = entry.conditions.strip() or "Дополнительные ограничения не указаны."
    return (
        "Тип источника: экспертное разъяснение службы качества\n"
        f"Обращение: {request.public_number}\n"
        f"Номер утверждения: {entry.approval_number}\n"
        f"Дата утверждения: {entry.approved_at.date().isoformat()}\n\n"
        f"Вопрос: {entry.original_question}\n\n"
        f"Утверждённое разъяснение: {entry.approved_text}\n\n"
        f"Область применения: {entry.scope}\n\n"
        f"Условия и ограничения: {conditions}"
    )


def publish_latest(db: Session, request_id: UUID, settings: Settings) -> tuple[QualityRequest, QualityKnowledgeEntry]:
    request = db.scalar(
        select(QualityRequest).where(QualityRequest.id == request_id).with_for_update()
    )
    if request is None:
        raise QualityServiceError(404, "Обращение не найдено.")
    entry = db.scalar(
        select(QualityKnowledgeEntry)
        .where(QualityKnowledgeEntry.quality_request_id == request.id)
        .order_by(QualityKnowledgeEntry.version.desc())
        .limit(1)
    )
    if entry is None or entry.status not in {"approved", "indexing_failed", "published"}:
        raise QualityServiceError(409, "Нет утверждённой версии для публикации.")
    if entry.status == "published":
        return request, entry
    text = _entry_text(request, entry)
    document = db.get(Document, entry.document_id) if entry.document_id else None
    if document is None:
        document_id = uuid4()
        storage_key = Path("quality") / str(entry.id) / "approved.txt"
        document = Document(
            id=document_id,
            title=entry.title,
            filename=f"quality-{request.public_number.lower()}-v{entry.version}.txt",
            storage_key=storage_key.as_posix(),
            status="indexing",
            active=False,
        )
        for ordinal, item in enumerate(chunk_text(text)):
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
        if not document.chunks:
            raise QualityServiceError(422, "Разъяснение не содержит индексируемого текста.")
        db.add(document)
        db.flush()
        entry.document_id = document.id
    else:
        document.status = "indexing"
        document.active = False
    entry.status = "publishing"
    request.updated_at = utcnow()
    db.commit()
    target = settings.storage_root / document.storage_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    try:
        index_chunks(document.chunks)
    except Exception as exc:
        document.active = False
        document.status = "indexing_failed"
        entry.status = "indexing_failed"
        request.status = "approved"
        request.updated_at = utcnow()
        _audit(
            db,
            request,
            "knowledge_indexing_failed",
            entry=entry,
            actor_label=entry.expert_label,
            authorization_method="demo_quality_approval_token",
            details={"error_category": type(exc).__name__},
        )
        db.commit()
        raise QualityServiceError(503, "Индексация не завершена; источник не опубликован.") from exc
    previous_entries = db.scalars(
        select(QualityKnowledgeEntry).where(
            QualityKnowledgeEntry.quality_request_id == request.id,
            QualityKnowledgeEntry.id != entry.id,
            QualityKnowledgeEntry.document_id.is_not(None),
        )
    ).all()
    for previous in previous_entries:
        previous_document = db.get(Document, previous.document_id)
        if previous_document is not None:
            previous_document.active = False
    now = utcnow()
    document.status = "ready"
    document.active = True
    entry.status = "published"
    entry.published_at = now
    request.status = "published"
    request.published_at = now
    request.updated_at = now
    _audit(
        db,
        request,
        "knowledge_published",
        entry=entry,
        actor_label=entry.expert_label,
        authorization_method="demo_quality_approval_token",
        details={"document_id": str(document.id)},
    )
    db.commit()
    return request, entry


def serialize_entry(entry: QualityKnowledgeEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "version": entry.version,
        "approval_number": entry.approval_number,
        "title": entry.title,
        "approved_text": entry.approved_text,
        "scope": entry.scope,
        "conditions": entry.conditions,
        "status": entry.status,
        "approval_method": entry.approval_method,
        "expert_label": entry.expert_label,
        "checksum": entry.checksum,
        "document_id": str(entry.document_id) if entry.document_id else None,
        "approved_at": entry.approved_at.isoformat(),
        "published_at": entry.published_at.isoformat() if entry.published_at else None,
    }


def serialize_request(db: Session, request: QualityRequest, *, detail: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(request.id),
        "public_number": request.public_number,
        "trace_id": str(request.trace_id),
        "conversation_id": str(request.conversation_id),
        "question": request.question,
        "reason_code": request.reason_code,
        "source_outcome": request.source_outcome,
        "status": request.status,
        "email_status": request.email_status,
        "email_error_category": request.email_error_category,
        "draft": {
            "title": request.draft_title or "",
            "answer": request.draft_answer or "",
            "scope": request.draft_scope or "",
            "conditions": request.draft_conditions or "",
            "expert_label": request.draft_expert_label or "",
        },
        "created_at": request.created_at.isoformat(),
        "updated_at": request.updated_at.isoformat(),
        "email_sent_at": request.email_sent_at.isoformat() if request.email_sent_at else None,
        "approved_at": request.approved_at.isoformat() if request.approved_at else None,
        "published_at": request.published_at.isoformat() if request.published_at else None,
    }
    if detail:
        result["clarification_context"] = request.clarification_context or {}
        entries = db.scalars(
            select(QualityKnowledgeEntry)
            .where(QualityKnowledgeEntry.quality_request_id == request.id)
            .order_by(QualityKnowledgeEntry.version.desc())
        ).all()
        result["knowledge_entries"] = [serialize_entry(entry) for entry in entries]
        audits = db.scalars(
            select(QualityAuditEvent)
            .where(QualityAuditEvent.quality_request_id == request.id)
            .order_by(QualityAuditEvent.created_at)
        ).all()
        result["audit"] = [
            {
                "action": row.action,
                "actor_label": row.actor_label,
                "authorization_method": row.authorization_method,
                "details": row.details,
                "created_at": row.created_at.isoformat(),
            }
            for row in audits
        ]
    return result
