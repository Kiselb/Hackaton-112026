from __future__ import annotations

import math
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import sentence_transformers
from sentence_transformers import CrossEncoder

from .config import get_settings


ScoreFunction = Callable[[str, list[dict[str, Any]]], list[float]]


@lru_cache
def cross_encoder() -> CrossEncoder:
    settings = get_settings()
    return CrossEncoder(
        settings.rerank_model,
        device=settings.rerank_device,
        revision=settings.rerank_model_revision or None,
    )


def _predict(question: str, candidates: list[dict[str, Any]]) -> list[float]:
    pairs = [(question, candidate["child_text"]) for candidate in candidates]
    raw_scores = cross_encoder().predict(
        pairs,
        batch_size=min(32, len(pairs)),
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return [float(score) for score in raw_scores]


def reranker_runtime_metadata(load_model: bool = False) -> dict[str, Any]:
    settings = get_settings()
    revision = settings.rerank_model_revision
    metadata_error = None
    if load_model:
        try:
            model = cross_encoder()
            revision = revision or getattr(model.model.config, "_commit_hash", None)
        except Exception as exc:
            metadata_error = f"{type(exc).__name__}: {exc}"
    return {
        "enabled": settings.rerank_enabled,
        "model": settings.rerank_model,
        "revision": revision or "not_resolved",
        "device": settings.rerank_device,
        "sentence_transformers_version": sentence_transformers.__version__,
        "metadata_error": metadata_error,
    }


def _fallback(
    candidates: list[dict[str, Any]],
    *,
    status: str,
    elapsed_ms: float,
    reason: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ranked = [
        {
            **candidate,
            "reranker_score": None,
            "rank_after_rerank": candidate["rank_before_rerank"],
            "score": candidate["rrf_score"],
        }
        for candidate in candidates
    ]
    return ranked, {
        **reranker_runtime_metadata(load_model=False),
        "status": status,
        "candidate_count": len(candidates),
        "latency_ms": round(elapsed_ms, 3),
        "fallback_reason": reason,
    }


def rerank_candidates(
    question: str,
    candidates: list[dict[str, Any]],
    *,
    enabled: bool | None = None,
    score_function: ScoreFunction | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = get_settings()
    is_enabled = settings.rerank_enabled if enabled is None else enabled
    if not candidates:
        return _fallback(
            candidates, status="skipped_empty", elapsed_ms=0.0, reason=None
        )
    if not is_enabled:
        return _fallback(candidates, status="disabled", elapsed_ms=0.0, reason=None)

    started = time.perf_counter()
    try:
        scores = (score_function or _predict)(question, candidates)
        if len(scores) != len(candidates):
            raise ValueError(
                f"reranker returned {len(scores)} scores for {len(candidates)} candidates"
            )
        if any(not math.isfinite(score) for score in scores):
            raise ValueError("reranker returned a non-finite score")
        scored = [
            {
                **candidate,
                "reranker_score": score,
                "score": score,
            }
            for candidate, score in zip(candidates, scores, strict=True)
        ]
        ranked = sorted(
            scored,
            key=lambda candidate: (
                -candidate["reranker_score"],
                candidate["rank_before_rerank"],
            ),
        )
        ranked = [
            {**candidate, "rank_after_rerank": rank}
            for rank, candidate in enumerate(ranked, start=1)
        ]
        elapsed_ms = (time.perf_counter() - started) * 1000
        return ranked, {
            **reranker_runtime_metadata(load_model=score_function is None),
            "status": "ok",
            "candidate_count": len(candidates),
            "latency_ms": round(elapsed_ms, 3),
            "fallback_reason": None,
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return _fallback(
            candidates,
            status="fallback",
            elapsed_ms=elapsed_ms,
            reason=f"{type(exc).__name__}: {exc}",
        )
