from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(500))
    filename: Mapped[str] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="indexing", index=True)
    storage_key: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    chunks: Mapped[list["Chunk"]] = relationship(cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_text: Mapped[str] = mapped_column(Text)
    child_text: Mapped[str] = mapped_column(Text)
    child_start: Mapped[int] = mapped_column(Integer)
    child_end: Mapped[int] = mapped_column(Integer)


class SearchTrace(Base):
    __tablename__ = "search_traces"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    question: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(String(200))
    session_id: Mapped[str] = mapped_column(String(200))
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="started")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraceFeedback(Base):
    __tablename__ = "trace_feedback"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    trace_id: Mapped[UUID] = mapped_column(ForeignKey("search_traces.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(200))
    session_id: Mapped[str] = mapped_column(String(200))
    rating: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_token_hash: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(32), default="processing", index=True)
    mode: Mapped[str] = mapped_column(String(32), default="agent")
    original_question: Mapped[str] = mapped_column(Text)
    pending_clarification: Mapped[str | None] = mapped_column(Text, nullable=True)
    clarification_context: Mapped[dict] = mapped_column(JSON, default=dict)
    clarification_count: Mapped[int] = mapped_column(Integer, default=0)
    user_answer_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    turns: Mapped[list["ConversationTurn"]] = relationship(
        cascade="all, delete-orphan", order_by="ConversationTurn.ordinal"
    )


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "ordinal", name="uq_conversation_turn_ordinal"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    trace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("search_traces.id"), nullable=True, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


engine = create_engine(get_settings().postgres_dsn, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
