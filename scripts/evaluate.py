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


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*.txt"))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def corpus_manifest() -> dict[str, Any]:
    files = {path.name: digest(path.read_bytes()) for path in EXAMPLES}
    return {
        "hash": digest(json.dumps(files, sort_keys=True).encode()),
        "files": files,
    }


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def prepare_environment(dataset_path: Path, corpus: dict[str, Any]) -> tuple[str, Path]:
    corpus_id = safe_name(dataset_path.stem)
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
        if saved != corpus:
            raise RuntimeError(
                "the versioned evaluation corpus differs from examples; "
                "create a new dataset version instead of mutating it"
            )
    return corpus_id, manifest_path


def git_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


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


def ensure_corpus(client: Any, manifest_path: Path, corpus: dict[str, Any]) -> str:
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
        return "reused"
    if manifest_path.exists():
        raise RuntimeError("corpus manifest exists but isolated database is empty")
    for path in EXAMPLES:
        upload = client.post(
            "/api/v1/documents",
            files={"file": (path.name, path.read_bytes(), "text/plain")},
            data={"title": path.stem.replace("_", " ")},
        )
        upload.raise_for_status()
    write_json(manifest_path, corpus)
    return "indexed"


def report_markdown(run: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [
        f"# Evaluation report: {run['experiment_id']}", "",
        f"Dataset: `{run['dataset']['id']}` / `{run['dataset']['version']}`  ",
        f"Git SHA: `{run['git_commit_sha']}`  ",
        f"Corpus SHA-256: `{run['corpus']['hash']}`", "",
        "## Aggregate", "", "```json",
        json.dumps(run["summary"], ensure_ascii=False, indent=2), "```", "",
        "Automatic fact conditions are narrow and do not prove semantic correctness "
        "or citation entailment. Marked cases remain unresolved pending human review.",
        "", "## Cases", "",
        "| Case | Category | R@5 | R@8 | MRR | Citations | Manual review | Error |",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    for item in results:
        metric = item["metrics"]
        lines.append(
            f"| {item['case_id']} | {item['category']} | "
            f"{metric['document_recall_at_5']} | {metric['document_recall_at_8']} | "
            f"{metric['mrr']} | {metric['citation_ids_valid']} | "
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
    dataset_path = Path(args.dataset).resolve()
    cases = load_dataset(dataset_path)
    corpus = corpus_manifest()
    corpus_id, manifest_path = prepare_environment(dataset_path, corpus)

    # These imports must see the isolated resource names set above.
    from fastapi.testclient import TestClient

    from clip_agent.config import get_settings
    from clip_agent.main import app

    settings = get_settings()
    started = datetime.now().astimezone()
    commit_sha = git_sha()
    experiment_id = f"{dataset_path.stem}-{started:%Y%m%dT%H%M%S%z}-{commit_sha[:8]}"
    output_dir = Path(args.output_dir).resolve() / experiment_id
    output_dir.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []
    with TestClient(app, raise_server_exceptions=False) as client:
        ready = readiness(client, settings)
        corpus_action = ensure_corpus(client, manifest_path, corpus)
        for case in cases:
            try:
                response = client.post(
                    "/api/v1/ask",
                    json={
                        "question": case["question"],
                        "user_id": "evaluation-lab",
                        "session_id": experiment_id,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                payload = {"answer": "", "sources": [], "metrics": {}, "error": str(exc)}
            result = evaluate_case(case, payload)
            results.append(result)
            status = "ERROR" if result["error"] else "ok"
            print(f"[{len(results):02d}/{len(cases):02d}] {case['case_id']}: {status}")

    finished = datetime.now().astimezone()
    dataset_parts = dataset_path.stem.rsplit("_", maxsplit=1)
    dataset_id = dataset_parts[0]
    dataset_version = dataset_parts[1] if len(dataset_parts) == 2 else "unversioned"
    run = {
        "format_version": "clip-evaluation-run/v1",
        "experiment_id": experiment_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "git_commit_sha": commit_sha,
        "dataset": {
            "id": dataset_id,
            "version": dataset_version,
            "path": str(dataset_path.relative_to(ROOT)),
            "sha256": digest(dataset_path.read_bytes()),
            "case_count": len(cases),
        },
        "corpus": {**corpus, "id": corpus_id, "preparation": corpus_action},
        "models": {
            "llm": settings.ollama_model,
            "generation": {
                "temperature": settings.ollama_temperature,
                "think": settings.ollama_think,
                "num_predict": settings.ollama_num_predict,
            },
            "embedding": settings.embedding_model,
            "embedding_device": settings.embedding_device,
        },
        "retrieval": {
            "strategy": "dense_bge_m3_plus_bm25_rrf",
            "dense_top_k": 50,
            "sparse_top_k": 50,
            "rrf_k": 60,
            "candidate_cap": 24,
            "selected_parents": 8,
            "qdrant_collection": settings.qdrant_collection,
            "opensearch_index": settings.opensearch_index,
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
    parser.add_argument("--output-dir", default="eval/results")
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
