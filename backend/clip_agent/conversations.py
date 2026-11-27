from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Conversation, ConversationTurn, utcnow


CONVERSATION_TOKEN_HEADER = "X-Conversation-Token"


class ConversationAccessError(RuntimeError):
    def __init__(self, status_code: int, message: str, reason_code: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason_code = reason_code


def new_owner_token() -> str:
    return secrets.token_urlsafe(32)


def hash_owner_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(conversation: Conversation, token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(conversation.owner_token_hash, hash_owner_token(token))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def is_expired(conversation: Conversation, now: datetime | None = None) -> bool:
    return _aware(conversation.expires_at) <= (now or datetime.now(UTC))


def create_conversation(
    db: Session,
    *,
    original_question: str,
    owner_id: str,
    settings: Settings,
) -> tuple[Conversation, str]:
    token = new_owner_token()
    conversation = Conversation(
        owner_token_hash=hash_owner_token(token),
        owner_id=owner_id,
        status="processing",
        mode="agent",
        original_question=original_question,
        clarification_context={"answers": []},
        expires_at=utcnow() + timedelta(minutes=settings.conversation_ttl_minutes),
    )
    db.add(conversation)
    db.flush()
    return conversation, token


def lock_conversation(
    db: Session,
    conversation_id: UUID,
    token: str | None,
    *,
    expected_status: str | None = None,
    allowed_statuses: tuple[str, ...] | None = None,
) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(Conversation.id == conversation_id).with_for_update()
    )
    if conversation is None or not token_matches(conversation, token):
        raise ConversationAccessError(404, "Диалог не найден.", "conversation_not_found")
    if is_expired(conversation):
        conversation.status = "expired"
        conversation.pending_clarification = None
        conversation.updated_at = utcnow()
        db.commit()
        raise ConversationAccessError(
            410,
            "Срок действия диалога истёк. Начните новый диалог.",
            "conversation_expired",
        )
    accepted_statuses = allowed_statuses or (
        (expected_status,) if expected_status is not None else None
    )
    if accepted_statuses and conversation.status not in accepted_statuses:
        if conversation.status == "processing":
            raise ConversationAccessError(
                409,
                "Предыдущий ответ этого диалога ещё обрабатывается.",
                "conversation_busy",
            )
        raise ConversationAccessError(
            409,
            "Этот диалог нельзя продолжить. Начните новый диалог.",
            "conversation_not_continuable",
        )
    return conversation


def next_ordinal(db: Session, conversation_id: UUID) -> int:
    current = db.scalar(
        select(func.max(ConversationTurn.ordinal)).where(
            ConversationTurn.conversation_id == conversation_id
        )
    )
    return int(current or 0) + 1


def add_turn(
    db: Session,
    conversation: Conversation,
    *,
    role: str,
    kind: str,
    content: str,
    trace_id: UUID | None = None,
    outcome: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> ConversationTurn:
    turn = ConversationTurn(
        conversation_id=conversation.id,
        trace_id=trace_id,
        ordinal=next_ordinal(db, conversation.id),
        role=role,
        kind=kind,
        content=content,
        outcome=outcome,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )
    db.add(turn)
    db.flush()
    return turn


def clarification_answers(conversation: Conversation) -> list[str]:
    context = conversation.clarification_context
    if not isinstance(context, dict) or not isinstance(context.get("answers"), list):
        return []
    return [str(value) for value in context["answers"] if str(value).strip()]


def append_clarification(
    conversation: Conversation, answer: str, *, max_context_chars: int
) -> list[str]:
    answers = [*clarification_answers(conversation), " ".join(answer.split())]
    bounded: list[str] = []
    total = 0
    for value in reversed(answers):
        cost = len(value) + (2 if bounded else 0)
        if total + cost > max_context_chars:
            break
        bounded.append(value)
        total += cost
    bounded.reverse()
    conversation.clarification_context = {"answers": bounded}
    return bounded


def resolved_query(conversation: Conversation) -> str:
    answers = clarification_answers(conversation)
    if not answers:
        return conversation.original_question
    conditions = "; ".join(answers)
    return (
        f"{conversation.original_question}\n"
        f"Уточнённые пользователем условия (контекст запроса, не корпоративные факты): "
        f"{conditions}"
    )


_REFERENCE_MARKER = re.compile(
    r"\b(?:его|её|ее|их|он|она|они|этот|эта|это|эти|такой|такая|такие|"
    r"указанн\w*|названн\w*|упомянут\w*)\b",
    re.IGNORECASE,
)
_DOCUMENT_OBJECT = re.compile(
    r"\b(?:регламент|приказ|положени|политик|инструкци|договор|документ|"
    r"заявк|лицензи|процедур)\w*\b",
    re.IGNORECASE,
)


def is_referential_question(question: str) -> bool:
    return bool(_REFERENCE_MARKER.search(" ".join(question.split())))


def bounded_reference_context(
    db: Session,
    conversation: Conversation,
    *,
    max_context_chars: int,
) -> str | None:
    """Return only the latest completed question exchange for pronoun resolution.

    The returned text is conversational context, never evidence. A chained reference or
    two distinct document-like objects is treated as ambiguous instead of being guessed.
    """
    root_turn = db.scalar(
        select(ConversationTurn)
        .where(
            ConversationTurn.conversation_id == conversation.id,
            ConversationTurn.role == "user",
            ConversationTurn.kind.in_(("original_question", "new_question")),
        )
        .order_by(ConversationTurn.ordinal.desc())
        .limit(1)
    )
    if root_turn is None or is_referential_question(root_turn.content):
        return None
    document_objects = {
        match.group(0).casefold() for match in _DOCUMENT_OBJECT.finditer(root_turn.content)
    }
    if len(document_objects) > 1:
        return None

    turns = list(
        db.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.ordinal >= root_turn.ordinal,
            )
            .order_by(ConversationTurn.ordinal)
        ).all()
    )
    labels = {"user": "Пользователь", "assistant": "Предыдущий ответ системы"}
    lines = [f"{labels.get(turn.role, turn.role)}: {' '.join(turn.content.split())}" for turn in turns]
    if not lines:
        return None

    root_line = lines[0][:max_context_chars]
    selected = [root_line]
    remaining = max_context_chars - len(root_line)
    for line in reversed(lines[1:]):
        if remaining <= 2:
            break
        excerpt = line[: remaining - 2]
        selected.insert(1, excerpt)
        remaining -= len(excerpt) + 2
    return "\n".join(selected)


def begin_new_question(conversation: Conversation, question: str) -> None:
    """Start an independent question while retaining immutable conversation turns."""
    conversation.original_question = question
    conversation.pending_clarification = None
    conversation.clarification_context = {"answers": []}
    conversation.clarification_count = 0
    conversation.user_answer_count = 0
    conversation.status = "processing"
    conversation.version += 1
    conversation.updated_at = utcnow()


def serialize_conversation(conversation: Conversation) -> dict[str, Any]:
    return {
        "conversation_id": str(conversation.id),
        "status": conversation.status,
        "mode": conversation.mode,
        "original_question": conversation.original_question,
        "pending_clarification": conversation.pending_clarification,
        "clarification_count": conversation.clarification_count,
        "user_answer_count": conversation.user_answer_count,
        "expires_at": conversation.expires_at.isoformat(),
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }
