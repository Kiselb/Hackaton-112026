from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from clip_agent.config import Settings
from clip_agent.evaluation import aggregate_results, compare_runs, evaluate_case, load_dataset
from clip_agent.extract import extract_text
from clip_agent.chunking import chunk_text


ROOT = Path(__file__).resolve().parents[1]


MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_corpus_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {"corpus_id", "version", "documents"}
    if missing := required - config.keys():
        raise ValueError(f"corpus config missing fields: {sorted(missing)}")
    if not config["documents"]:
        raise ValueError("corpus config has no documents")
    source_ids: set[str] = set()
    filenames: set[str] = set()
    fragment_ids: set[str] = set()
    for document in config["documents"]:
        for field in ("source_id", "filename", "title", "path"):
            if field not in document:
                raise ValueError(f"corpus document missing {field!r}")
        passages = document.get("passages", document.get("fragments"))
        if passages is None:
            raise ValueError("corpus document missing 'passages' or legacy 'fragments'")
        if document["source_id"] in source_ids or document["filename"] in filenames:
            raise ValueError("corpus source_id and filename must be unique")
        source_ids.add(document["source_id"])
        filenames.add(document["filename"])
        document_path = (path.parent / document["path"]).resolve()
        if not document_path.is_relative_to(ROOT):
            raise ValueError(f"corpus document is outside repository: {document_path}")
        text_value = extract_text(document["filename"], document_path.read_bytes())
        if not text_value.strip():
            raise ValueError(f"corpus document has no extractable text: {document['filename']}")
        for fragment in passages:
            fragment_id = fragment.get("passage_id", fragment.get("fragment_id"))
            quote = fragment.get("quote")
            if not fragment_id or fragment_id in fragment_ids:
                raise ValueError("corpus passage_id must be non-empty and unique")
            if not quote or quote not in text_value:
                raise ValueError(
                    f"gold fragment {fragment_id!r} is not present in {document['filename']}"
                )
            fragment_ids.add(fragment_id)
    return config


def corpus_manifest(config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    files = {
        document["filename"]: digest((config_path.parent / document["path"]).read_bytes())
        for document in config["documents"]
    }
    config_sha256 = digest(config_path.read_bytes())
    return {
        "hash": digest(
            json.dumps(
                {"config_sha256": config_sha256, "files": files}, sort_keys=True
            ).encode()
        ),
        "config_sha256": config_sha256,
        "files": files,
    }


def corpus_inventory(config_path: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    inventory = []
    for document in config["documents"]:
        path = (config_path.parent / document["path"]).resolve()
        data = path.read_bytes()
        extracted = extract_text(document["filename"], data)
        inventory.append(
            {
                "source_id": document["source_id"],
                "filename": document["filename"],
                "sha256": digest(data),
                "size_bytes": len(data),
                "extraction_succeeded": bool(extracted.strip()),
                "extracted_chars": len(extracted),
                "expected_chunk_count": len(chunk_text(extracted)),
            }
        )
    return inventory


def corpus_manifests_match(saved: dict[str, Any], current: dict[str, Any]) -> bool:
    if saved == current:
        return True
    legacy = {
        "hash": digest(json.dumps(current["files"], sort_keys=True).encode()),
        "files": current["files"],
    }
    return saved == legacy


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def prepare_environment(config: dict[str, Any], corpus: dict[str, Any]) -> tuple[str, Path]:
    corpus_id = safe_name(config["corpus_id"])
    schema = f"clip_eval_{corpus_id}"
    base = Settings()
    engine = create_engine(base.postgres_dsn, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    engine.dispose()
    isolated_url = make_url(base.postgres_dsn).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    runtime = ROOT / "eval" / "runtime" / corpus_id
    os.environ.update(
        {
            "POSTGRES_DSN": isolated_url.render_as_string(hide_password=False),
            "QDRANT_COLLECTION": f"clip_agent_eval_{corpus_id}_dense",
            "OPENSEARCH_INDEX": f"clip_agent_eval_{corpus_id}_sparse",
            "STORAGE_ROOT": str(runtime / "storage"),
        }
    )
    runtime.mkdir(parents=True, exist_ok=True)
    manifest_path = runtime / "corpus_manifest.json"
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not corpus_manifests_match(saved, corpus):
            raise RuntimeError(
                "the versioned evaluation corpus differs from its config; "
                "create a new dataset version instead of mutating it"
            )
    return corpus_id, manifest_path


def git_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def source_state() -> dict[str, Any]:
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).split(b"\0")
    source_hash = hashlib.sha256()
    file_count = 0
    for raw_path in sorted(path for path in paths if path):
        relative = raw_path.decode("utf-8")
        path = ROOT / relative
        if not path.is_file():
            continue
        source_hash.update(relative.replace("\\", "/").encode("utf-8"))
        source_hash.update(b"\0")
        source_hash.update(path.read_bytes())
        source_hash.update(b"\0")
        file_count += 1
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
    return {
        "sha256": source_hash.hexdigest(),
        "file_count": file_count,
        "dirty": bool(status.strip()),
        "status_porcelain": status.splitlines(),
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_file(value: str) -> Path:
    path = Path(value)
    return path / "run.json" if path.is_dir() else path


def comparison_markdown(comparison: dict[str, Any]) -> str:
    lines = [
        "# Experiment comparison", "",
        f"Left: `{comparison['left_experiment_id']}`  ",
        f"Right: `{comparison['right_experiment_id']}`", "",
        f"Same dataset: **{comparison['same_dataset']}**  ",
        f"Same corpus: **{comparison['same_corpus_hash']}**", "",
        "| Metric | Left | Right | Delta |",
        "|---|---:|---:|---:|",
    ]
    for name, values in comparison["metrics"].items():
        lines.append(
            f"| {name} | {values['left']} | {values['right']} | {values['delta']} |"
        )
    return "\n".join(lines) + "\n"


def compare_command(paths: list[str], output: Path | None) -> int:
    left = json.loads(run_file(paths[0]).read_text(encoding="utf-8"))
    right = json.loads(run_file(paths[1]).read_text(encoding="utf-8"))
    comparison = compare_runs(left, right)
    rendered = comparison_markdown(comparison)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        write_json(output.with_suffix(".json"), comparison)
    print(rendered)
    return 0 if comparison["same_dataset"] and comparison["same_corpus_hash"] else 2


def readiness(client: Any, settings: Settings) -> dict[str, Any]:
    response = client.get("/readyz")
    response.raise_for_status()
    installed = httpx.get(f"{settings.ollama_url}/api/tags", timeout=10)
    installed.raise_for_status()
    models = {item["name"] for item in installed.json().get("models", [])}
    if settings.ollama_model not in models:
        raise RuntimeError(f"Ollama model {settings.ollama_model!r} is not installed")
    return response.json()


def ensure_corpus(
    client: Any,
    manifest_path: Path,
    corpus: dict[str, Any],
    config_path: Path,
    config: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    response = client.get("/api/v1/documents")
    response.raise_for_status()
    documents = response.json()
    expected_names = set(corpus["files"])
    observed_names = {item["filename"] for item in documents}
    if documents:
        if not manifest_path.exists() or observed_names != expected_names:
            raise RuntimeError(
                "isolated corpus is partial or lacks a matching manifest; nothing was changed"
            )
        if any(item["status"] != "ready" or not item["active"] for item in documents):
            raise RuntimeError("isolated corpus contains non-ready or inactive documents")
        return "reused", documents
    if manifest_path.exists():
        raise RuntimeError("corpus manifest exists but isolated database is empty")
    for document in config["documents"]:
        path = (config_path.parent / document["path"]).resolve()
        upload = client.post(
            "/api/v1/documents",
            files={
                "file": (
                    document["filename"],
                    path.read_bytes(),
                    MIME_TYPES.get(path.suffix.lower(), "application/octet-stream"),
                )
            },
            data={"title": document["title"]},
        )
        upload.raise_for_status()
    write_json(manifest_path, corpus)
    indexed = client.get("/api/v1/documents")
    indexed.raise_for_status()
    return "indexed", indexed.json()


def validate_gold(cases: list[dict[str, Any]], config: dict[str, Any]) -> None:
    source_ids = {document["source_id"] for document in config["documents"]}
    fragment_ids = {
        fragment.get("passage_id", fragment.get("fragment_id"))
        for document in config["documents"]
        for fragment in document.get("passages", document.get("fragments", []))
    }
    for case in cases:
        unknown_sources = (
            set(case["expected_sources"]) | set(case.get("relevance_grades", {}))
        ) - source_ids
        unknown_fragments = set(
            case.get("expected_passages", case.get("expected_fragments", []))
        ) - fragment_ids
        if unknown_sources or unknown_fragments:
            raise ValueError(
                f"{case['case_id']}: unknown gold sources={sorted(unknown_sources)}, "
                f"fragments={sorted(unknown_fragments)}"
            )


def annotate_sources(payload: dict[str, Any], config: dict[str, Any]) -> None:
    documents = {document["filename"]: document for document in config["documents"]}
    for source in payload.get("sources", []):
        document = documents.get(source.get("filename"))
        if not document:
            continue
        source["source_id"] = document["source_id"]
        evidence = source.get("parent_text") or source.get("child_text", "")
        passages = document.get("passages", document.get("fragments", []))
        passage_ids = [
            fragment.get("passage_id", fragment.get("fragment_id"))
            for fragment in passages
            if fragment["quote"] in evidence
        ]
        source["passage_ids"] = passage_ids
        source["fragment_ids"] = passage_ids
        normalized_evidence = " ".join(evidence.split())
        source["retrieval_passage_id"] = (
            f"retrieved:{digest((document['source_id'] + chr(0) + normalized_evidence).encode())[:20]}"
        )


def _combined_metrics(*payloads: dict[str, Any]) -> dict[str, Any]:
    metrics = [payload.get("metrics", {}) for payload in payloads]
    summed = {}
    for key in (
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "agent_search_calls",
        "agent_llm_calls",
        "llm_calls",
        "agent_steps",
        "multi_query_search_calls",
        "assessment_latency_ms",
        "assessment_malformed_responses",
        "assessment_empty_responses",
        "assessment_truncated_responses",
    ):
        values = [item.get(key) for item in metrics if item.get(key) is not None]
        summed[key] = sum(values) if values else None
    if metrics:
        summed.update(
            {
                key: metrics[-1].get(key)
                for key in (
                    "rerank_latency_ms",
                    "multi_query_effective",
                    "multi_query_unique_sources",
                )
            }
        )
    return summed


def _assessment_trace_metrics(client: Any, trace_ids: list[str | None]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for trace_id in trace_ids:
        if not trace_id:
            continue
        response = client.get(f"/api/v1/admin/traces/{trace_id}")
        response.raise_for_status()
        details = response.json().get("details", {})
        events.extend(
            event
            for event in details.get("trajectory", [])
            if event.get("node") == "assess_evidence"
        )
    diagnostics = [event.get("structured_output", {}) for event in events]
    return {
        "assessment_latency_ms": round(
            sum(float(event.get("latency_ms") or 0) for event in events), 4
        ),
        "assessment_malformed_responses": sum(
            item.get("rejection_reason") == "malformed_json" for item in diagnostics
        ),
        "assessment_empty_responses": sum(
            item.get("rejection_reason") == "empty_response" for item in diagnostics
        ),
        "assessment_truncated_responses": sum(
            bool(item.get("output_limit_reached")) for item in diagnostics
        ),
    }


def execute_case(
    client: Any,
    case: dict[str, Any],
    *,
    experiment_id: str,
    multi_query: bool,
    agent_enabled: bool,
) -> dict[str, Any]:
    request = {
        "question": case["question"],
        "user_id": "evaluation-lab",
        "session_id": experiment_id,
        "multi_query_enabled": multi_query,
    }
    initial_response = client.post("/api/v1/ask", json=request)
    initial_response.raise_for_status()
    initial = initial_response.json()
    initial.setdefault("metrics", {}).update(
        _assessment_trace_metrics(client, [initial.get("trace_id")])
    )
    dialogue_spec = case.get("dialogue")
    if not dialogue_spec:
        return initial

    dialogue = {
        "initial_outcome": initial.get("outcome"),
        "initial_answer": initial.get("answer"),
        "initial_metrics": initial.get("metrics", {}),
        "clarification_question": initial.get("answer"),
        "user_reply": dialogue_spec.get("user_reply"),
        "expected_final_outcome": dialogue_spec.get("expected_final_outcome"),
        "turn_count": 1,
        "trace_ids": [initial.get("trace_id")],
    }
    if not agent_enabled or initial.get("outcome") != "clarification_required":
        initial["dialogue"] = {**dialogue, "final_outcome": initial.get("outcome")}
        return initial

    token = initial_response.headers.get("x-conversation-token")
    conversation_id = initial.get("conversation_id")
    if not token or not conversation_id or not dialogue_spec.get("user_reply"):
        raise RuntimeError("clarification response lacks token, conversation_id, or user reply")
    follow_up_response = client.post(
        "/api/v1/ask",
        headers={"X-Conversation-Token": token},
        json={
            **request,
            "question": dialogue_spec["user_reply"],
            "conversation_id": conversation_id,
        },
    )
    follow_up_response.raise_for_status()
    final = follow_up_response.json()
    final.setdefault("metrics", {}).update(
        _assessment_trace_metrics(client, [final.get("trace_id")])
    )
    final["metrics"] = _combined_metrics(initial, final)
    final["dialogue"] = {
        **dialogue,
        "final_outcome": final.get("outcome"),
        "final_answer": final.get("answer"),
        "final_metrics": follow_up_response.json().get("metrics", {}),
        "turn_count": 2,
        "trace_ids": [initial.get("trace_id"), final.get("trace_id")],
    }
    return final


def report_markdown(run: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [
        f"# Evaluation report: {run['experiment_id']}", "",
        f"Dataset: `{run['dataset']['id']}` / `{run['dataset']['version']}`  ",
        f"Git SHA: `{run['git_commit_sha']}`  ",
        f"Source tree SHA-256: `{run['source_state']['sha256']}`  ",
        f"Dirty worktree: **{run['source_state']['dirty']}**  ",
        f"Corpus SHA-256: `{run['corpus']['hash']}`  ",
        f"Gold status: **{run['dataset']['gold_status']}**  ",
        f"Mode: **{run['mode']}** / Multi-Query: **{run['agent']['multi_query_requested']}**",
        "", "## Corpus", "",
        "| Source | Bytes | SHA-256 | Extracted chars | Chunks | Document ID |",
        "|---|---:|---|---:|---:|---|",
    ]
    for item in run["corpus"]["documents"]:
        lines.append(
            f"| {item['filename']} | {item['size_bytes']} | `{item['sha256']}` | "
            f"{item['extracted_chars']} | {item['indexed_chunk_count']} | "
            f"`{item['document_id']}` |"
        )
    lines.extend([
        "", "## Aggregate", "", "```json",
        json.dumps(run["summary"], ensure_ascii=False, indent=2), "```", "",
        "Automatic fact conditions are narrow and do not prove semantic correctness "
        "or citation entailment. Marked cases remain unresolved pending human review.",
        "", "## Cases", "",
        "| Case | Category | Doc R@1 | Doc R@3 | Passage R@5 | Passage P@5 | MRR | nDCG@3 | Outcome | Citations | Manual review | Error |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|",
    ])
    for item in results:
        metric = item["metrics"]
        lines.append(
            f"| {item['case_id']} | {item['category']} | "
            f"{metric['document_recall_at_1']} | {metric['document_recall_at_3']} | "
            f"{metric['passage_recall_at_5']} | {metric['passage_precision_at_5']} | "
            f"{metric['mrr']} | {metric['ndcg_at_3']} | {metric['outcome']} | "
            f"{metric['citation_ids_valid']} | "
            f"{item['manual_review_required']} | {item['error'] or ''} |"
        )
    lines.extend(["", "## Case details", ""])
    for item in results:
        lines.extend(
            [
                f"### {item['case_id']} — {item['category']}",
                "",
                f"Question: {item['question']}",
                "",
                f"Expected: {', '.join(item['expected_sources']) or '(none)'}  ",
                f"Retrieved: {', '.join(item['retrieved_sources']) or '(none)'}  ",
                f"Manual review: {item['manual_review_required']}  ",
                f"Error: {item['error'] or '(none)'}",
                "",
                "Answer:",
                "",
                item["answer"] or "(empty)",
                "",
                "Deterministic checks:",
                "",
                "    " + json.dumps(item["metrics"], ensure_ascii=False),
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def evaluate(args: argparse.Namespace) -> int:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(f"Python 3.12 is required, got {platform.python_version()}")
    if args.mode == "baseline" and args.multi_query:
        raise ValueError("Multi-Query cannot be requested in baseline mode")
    if args.mode:
        os.environ["AGENT_ENABLED"] = str(args.mode == "agent").lower()
    os.environ["QUALITY_EMAIL_ENABLED"] = "false"
    dataset_path = Path(args.dataset).resolve()
    config_path = (
        Path(args.corpus_config).resolve()
        if args.corpus_config
        else ROOT / "eval" / "corpora" / dataset_path.stem / "corpus.json"
    )
    cases = load_dataset(dataset_path)
    config = load_corpus_config(config_path)
    validate_gold(cases, config)
    corpus = corpus_manifest(config_path, config)
    source_inventory = corpus_inventory(config_path, config)
    corpus_id, manifest_path = prepare_environment(config, corpus)

    # These imports must see the isolated resource names set above.
    from fastapi.testclient import TestClient

    from clip_agent.config import get_settings
    from clip_agent.main import app
    from clip_agent.reranking import reranker_runtime_metadata

    settings = get_settings()
    started = datetime.now().astimezone()
    commit_sha = git_sha()
    actual_source = source_state()
    experiment_id = (
        f"{dataset_path.stem}-{started:%Y%m%dT%H%M%S%z}-"
        f"{commit_sha[:8]}-{actual_source['sha256'][:8]}"
    )
    output_dir = Path(args.output_dir).resolve() / experiment_id
    output_dir.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []
    with TestClient(app, raise_server_exceptions=False) as client:
        ready = readiness(client, settings)
        corpus_action, indexed_documents = ensure_corpus(
            client, manifest_path, corpus, config_path, config
        )
        indexed_by_name = {item["filename"]: item for item in indexed_documents}
        for case in cases:
            try:
                payload = execute_case(
                    client,
                    case,
                    experiment_id=experiment_id,
                    multi_query=args.multi_query,
                    agent_enabled=settings.agent_enabled,
                )
                annotate_sources(payload, config)
            except Exception as exc:
                payload = {"answer": "", "sources": [], "metrics": {}, "error": str(exc)}
            result = evaluate_case(case, payload)
            results.append(result)
            status = "ERROR" if result["error"] else "ok"
            print(f"[{len(results):02d}/{len(cases):02d}] {case['case_id']}: {status}")

    finished = datetime.now().astimezone()
    reranker_metadata = reranker_runtime_metadata(
        load_model=settings.rerank_enabled
    )
    dataset_parts = dataset_path.stem.rsplit("_", maxsplit=1)
    dataset_id = dataset_parts[0]
    dataset_version = dataset_parts[1] if len(dataset_parts) == 2 else "unversioned"
    gold_statuses = {case.get("gold_status", "draft") for case in cases}
    corpus_documents = []
    for item in source_inventory:
        indexed = indexed_by_name[item["filename"]]
        corpus_documents.append(
            {
                **item,
                "document_id": indexed["id"],
                "indexed_chunk_count": indexed["chunks"],
                "status": indexed["status"],
                "active": indexed["active"],
            }
        )
    run = {
        "format_version": "clip-evaluation-run/v2",
        "experiment_id": experiment_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "git_commit_sha": commit_sha,
        "source_state": actual_source,
        "dataset": {
            "id": dataset_id,
            "version": dataset_version,
            "path": str(dataset_path.relative_to(ROOT)),
            "sha256": digest(dataset_path.read_bytes()),
            "case_count": len(cases),
            "gold_status": (
                next(iter(gold_statuses)) if len(gold_statuses) == 1 else "mixed"
            ),
        },
        "corpus": {
            **corpus,
            "id": corpus_id,
            "version": config["version"],
            "config_path": str(config_path.relative_to(ROOT)),
            "document_count": len(config["documents"]),
            "preparation": corpus_action,
            "documents": corpus_documents,
        },
        "mode": "agent" if settings.agent_enabled else "baseline",
        "models": {
            "provider": settings.llm_default_provider,
            "llm": settings.ollama_model,
            "generation": {
                "temperature": settings.ollama_temperature,
                "think": settings.ollama_think,
                "num_predict": settings.ollama_num_predict,
            },
            "embedding": settings.embedding_model,
            "embedding_device": settings.embedding_device,
            "reranker": reranker_metadata,
        },
        "retrieval": {
            "strategy": (
                "bounded_langgraph_dense_bm25_rrf"
                if settings.agent_enabled
                else "dense_bge_m3_plus_bm25_rrf_cross_encoder"
                if settings.rerank_enabled
                else "dense_bge_m3_plus_bm25_rrf"
            ),
            "dense_top_k": 50,
            "sparse_top_k": 50,
            "rrf_k": 60,
            "candidate_cap": settings.rerank_candidates,
            "selected_parents": settings.rerank_top_k,
            "qdrant_collection": settings.qdrant_collection,
            "opensearch_index": settings.opensearch_index,
        },
        "agent": {
            "enabled": settings.agent_enabled,
            "max_search_calls": settings.agent_max_search_calls,
            "max_retries": settings.agent_max_retries,
            "max_llm_calls": settings.agent_max_llm_calls,
            "assessment_max_output_tokens": (
                settings.agent_assessment_max_output_tokens
            ),
            "composition_max_output_tokens": (
                settings.agent_composition_max_output_tokens
            ),
            "validation_max_output_tokens": (
                settings.agent_validation_max_output_tokens
            ),
            "context_window_tokens": settings.agent_context_window_tokens,
            "multi_query_requested": args.multi_query,
            "multi_query_max_variants": settings.multi_query_max_variants,
        },
        "readiness": ready,
        "summary": aggregate_results(results),
        "artifacts": {"cases": "cases.jsonl", "report": "report.md"},
    }
    write_json(output_dir / "run.json", run)
    (output_dir / "cases.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(report_markdown(run, results), encoding="utf-8")
    print(f"Results: {output_dir}")
    return 1 if run["summary"]["error_count"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CLIP deterministic RAG evaluation lab")
    parser.add_argument("--dataset", help="JSONL evaluation dataset")
    parser.add_argument(
        "--corpus-config",
        help="Explicit corpus JSON; defaults to eval/corpora/<dataset stem>/corpus.json",
    )
    parser.add_argument("--output-dir", default="eval/results")
    parser.add_argument(
        "--mode",
        choices=("baseline", "agent"),
        help="Explicitly set the evaluation mode without changing .env",
    )
    parser.add_argument(
        "--multi-query",
        action="store_true",
        help="Request Multi-Query for every AI-Agent case in this run",
    )
    parser.add_argument("--compare", nargs=2, metavar=("LEFT_RUN", "RIGHT_RUN"))
    parser.add_argument("--comparison-output", type=Path)
    args = parser.parse_args()
    if args.compare:
        return compare_command(args.compare, args.comparison_output)
    if not args.dataset:
        parser.error("--dataset is required unless --compare is used")
    return evaluate(args)


if __name__ == "__main__":
    raise SystemExit(main())
