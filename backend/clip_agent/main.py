import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .agent import run_agent
from .answering import (
    build_answer_prompt,
    build_citation_repair_prompt,
    build_temporal_abstention,
    citation_ids_admissible,
    merge_ollama_usage,
    requires_temporal_abstention,
)
from .chunking import chunk_text
from .config import get_settings
from .db import Chunk, Document, SearchTrace, SessionLocal, init_db
from .extract import extract_text
from .search import index_chunks, opensearch, qdrant, retrieve

settings = get_settings()
app = FastAPI(title="AI-Agent Clip", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    with SessionLocal() as session:
        yield session


@app.on_event("startup")
def startup() -> None:
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    init_db()


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=4000)
    user_id: str = "demo-user"
    session_id: str = "demo-session"


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz(db: Session = Depends(get_db)) -> dict:
    checks: dict[str, str] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"
    for name, check in (
        ("qdrant", lambda: qdrant().get_collections()),
        ("opensearch", lambda: opensearch().cluster.health()),
        ("ollama", lambda: check_ollama_model()),
    ):
        try:
            check()
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = f"error: {exc}"
    if any(value != "ok" for value in checks.values()):
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
    file: UploadFile = File(...), title: str | None = Form(None), db: Session = Depends(get_db)
) -> dict:
    data = file.file.read()
    if not data or len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "Пустой файл или размер больше 25 MB")
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
        raise HTTPException(500, detail=f"Индексация не выполнена: {exc}") from exc
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


@app.post("/api/v1/ask")
def ask(payload: AskRequest, db: Session = Depends(get_db)) -> dict:
    started = time.perf_counter()
    trace = SearchTrace(
        question=payload.question, user_id=payload.user_id, session_id=payload.session_id
    )
    db.add(trace)
    db.commit()
    agent_fallback_reason = None
    if settings.agent_enabled:
        try:
            result = run_agent(
                payload.question,
                payload.user_id,
                payload.session_id,
                str(trace.id),
            )
        except Exception as exc:
            agent_fallback_reason = str(exc)
        else:
            latency_ms = round((time.perf_counter() - started) * 1000)
            sources = result["sources"]
            usage = result["usage"]
            retrieval_trace = result["retrieval"]
            details = result["trace_details"]
            validation = details.get("validation", {})
            trace.answer = result["answer"]
            trace.retrieved_count = result["retrieved_count"]
            trace.selected_count = len(sources)
            trace.prompt_tokens = usage.get("prompt_eval_count")
            trace.completion_tokens = usage.get("eval_count")
            trace.latency_ms = latency_ms
            trace.status = "ok"
            trace.details = details
            db.commit()
            return {
                "answer": result["answer"],
                "sources": sources,
                "retrieval": retrieval_trace,
                "trace_id": str(trace.id),
                "metrics": {
                    "latency_ms": latency_ms,
                    "prompt_tokens": trace.prompt_tokens,
                    "completion_tokens": trace.completion_tokens,
                    "retrieved_count": result["retrieved_count"],
                    "selected_count": len(sources),
                    "done_reason": None,
                    "citations_valid": validation.get("citations_valid", False),
                    "empty_answer": not bool(result["answer"]),
                    "citation_repair_attempted": validation.get(
                        "citation_repair_attempted", False
                    ),
                    "temporal_guard_applied": (
                        details.get("refusal_reason") == "temporal_evidence_gap"
                    ),
                    "rerank_status": retrieval_trace["status"],
                    "rerank_latency_ms": retrieval_trace["latency_ms"],
                    "rerank_fallback_reason": retrieval_trace["fallback_reason"],
                    "agent_enabled": True,
                    "agent_search_calls": details.get("search_calls", 0),
                    "agent_llm_calls": details.get("llm_calls", 0),
                    "agent_steps": details.get("step_count", 0),
                    "agent_retries": details.get("retries", 0),
                },
            }
    citation_repair_attempted = False
    citation_repair_error = None
    temporal_guard_applied = False
    try:
        sources, retrieved_count, retrieval_trace = retrieve(payload.question)
        if not sources:
            answer = "В активных документах не найдено достаточно данных для ответа."
            usage = {}
        elif requires_temporal_abstention(payload.question, sources):
            answer = build_temporal_abstention(sources)
            usage = {}
            citations_valid = citation_ids_admissible(answer, len(sources))
            temporal_guard_applied = True
        else:
            prompt = build_answer_prompt(payload.question, sources)
            response = httpx.post(
                f"{settings.ollama_url}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "stream": False,
                    "think": settings.ollama_think,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {
                        "temperature": settings.ollama_temperature,
                        "num_predict": settings.ollama_num_predict,
                    },
                },
                timeout=300,
            )
            response.raise_for_status()
            usage = response.json()
            answer = usage["message"]["content"].strip()
            if not answer:
                latency_ms = round((time.perf_counter() - started) * 1000)
                trace.answer = ""
                trace.retrieved_count = retrieved_count
                trace.selected_count = len(sources)
                trace.prompt_tokens = usage.get("prompt_eval_count")
                trace.completion_tokens = usage.get("eval_count")
                trace.latency_ms = latency_ms
                trace.status = "empty"
                trace.details = {
                    "mode": "baseline",
                    "agent_fallback_reason": agent_fallback_reason,
                    "source_ids": [source["chunk_id"] for source in sources],
                    "done_reason": usage.get("done_reason"),
                    "ollama": ollama_metrics(usage),
                    "reranking": retrieval_trace,
                }
                db.commit()
                raise HTTPException(
                    503,
                    detail={
                        "message": "Ollama returned an empty answer",
                        "done_reason": usage.get("done_reason"),
                    },
                )
            citations_valid = citation_ids_admissible(answer, len(sources))
            if not citations_valid:
                citation_repair_attempted = True
                try:
                    repair_response = httpx.post(
                        f"{settings.ollama_url}/api/chat",
                        json={
                            "model": settings.ollama_model,
                            "stream": False,
                            "think": settings.ollama_think,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": build_citation_repair_prompt(
                                        prompt, answer, len(sources)
                                    ),
                                }
                            ],
                            "options": {
                                "temperature": settings.ollama_temperature,
                                "num_predict": settings.ollama_num_predict,
                            },
                        },
                        timeout=300,
                    )
                    repair_response.raise_for_status()
                    repair_usage = repair_response.json()
                    usage = merge_ollama_usage(usage, repair_usage)
                    repaired_answer = repair_usage["message"]["content"].strip()
                    if repaired_answer:
                        answer = repaired_answer
                except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                    citation_repair_error = str(exc)
                citations_valid = citation_ids_admissible(answer, len(sources))
            if not citations_valid:
                answer += (
                    "\n\nПримечание: автоматическая проверка не подтвердила "
                    "корректные номера цитат."
                )
        latency_ms = round((time.perf_counter() - started) * 1000)
        trace.answer = answer
        trace.retrieved_count = retrieved_count
        trace.selected_count = len(sources)
        trace.prompt_tokens = usage.get("prompt_eval_count")
        trace.completion_tokens = usage.get("eval_count")
        trace.latency_ms = latency_ms
        trace.status = "ok"
        trace.details = {
            "mode": "baseline",
            "agent_fallback_reason": agent_fallback_reason,
            "source_ids": [source["chunk_id"] for source in sources],
            "done_reason": usage.get("done_reason"),
            "citations_valid": citations_valid if sources else False,
            "empty_answer": False,
            "citation_repair_attempted": citation_repair_attempted,
            "temporal_guard_applied": temporal_guard_applied,
            "citation_repair_error": citation_repair_error,
            "reranking": retrieval_trace,
            "ollama": ollama_metrics(usage),
        }
        db.commit()
        return {
            "answer": answer,
            "sources": sources,
            "retrieval": retrieval_trace,
            "trace_id": str(trace.id),
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
                "rerank_fallback_reason": retrieval_trace["fallback_reason"],
                "agent_enabled": False,
                "agent_fallback_reason": agent_fallback_reason,
                **ollama_metrics(usage),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        trace.status = "failed"
        trace.latency_ms = round((time.perf_counter() - started) * 1000)
        trace.details = {
            "mode": "baseline",
            "agent_fallback_reason": agent_fallback_reason,
            "error": str(exc),
        }
        db.commit()
        raise HTTPException(503, detail=f"Локальный RAG недоступен: {exc}") from exc


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


@app.get("/api/v1/admin/analytics")
def analytics(db: Session = Depends(get_db)) -> dict:
    total_documents = db.scalar(select(func.count()).select_from(Document)) or 0
    active_documents = (
        db.scalar(select(func.count()).select_from(Document).where(Document.active.is_(True))) or 0
    )
    total_queries = db.scalar(select(func.count()).select_from(SearchTrace)) or 0
    failed_queries = (
        db.scalar(
            select(func.count()).select_from(SearchTrace).where(SearchTrace.status == "failed")
        )
        or 0
    )
    average_latency_ms = db.scalar(select(func.avg(SearchTrace.latency_ms)))
    return {
        "total_documents": total_documents,
        "active_documents": active_documents,
        "total_queries": total_queries,
        "failed_queries": failed_queries,
        "average_latency_ms": round(float(average_latency_ms or 0), 1),
    }


@app.get("/api/v1/admin/traces")
def traces(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(SearchTrace).order_by(SearchTrace.created_at.desc()).limit(50)).all()
    return [
        {
            "id": str(row.id),
            "question": row.question,
            "user_id": row.user_id,
            "session_id": row.session_id,
            "status": row.status,
            "latency_ms": row.latency_ms,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "retrieved_count": row.retrieved_count,
            "selected_count": row.selected_count,
            "details": row.details,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
