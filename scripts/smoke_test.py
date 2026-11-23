import argparse
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx
import psutil


QUESTION = "Какие пени применяются за просрочку предоплаты и почему в документах указаны разные ставки?"
RATE_101 = re.compile(r"0[,.]1(?:0)?\s*(?:%|процент)", re.IGNORECASE)
RATE_102 = re.compile(r"0[,.]05\s*(?:%|процент)", re.IGNORECASE)


def gpu_snapshot() -> dict | None:
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        ).strip()
        name, used, total, utilization = [part.strip() for part in output.split(",")]
        return {
            "name": name,
            "vram_used_mib": int(used),
            "vram_total_mib": int(total),
            "gpu_utilization_percent": int(utilization),
        }
    except Exception:
        return None


def resource_snapshot(client: httpx.Client, ollama_url: str) -> dict:
    try:
        ollama_ps = client.get(f"{ollama_url}/api/ps", timeout=10).json()
    except Exception as exc:
        ollama_ps = {"error": str(exc)}
    return {
        "captured_at": datetime.now().astimezone().isoformat(),
        "gpu": gpu_snapshot(),
        "system_ram_used_mib": round(psutil.virtual_memory().used / 1024 / 1024),
        "ollama_ps": ollama_ps,
    }


def unload_model(client: httpx.Client, ollama_url: str, model: str) -> dict:
    response = client.post(
        f"{ollama_url}/api/generate",
        json={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
        timeout=60,
    )
    response.raise_for_status()
    return {
        "status_code": response.status_code,
        "done_reason": response.json().get("done_reason"),
    }


def ask(client: httpx.Client, base_url: str) -> tuple[dict, float]:
    started = time.perf_counter()
    response = client.post(
        f"{base_url}/api/v1/ask",
        json={"question": QUESTION, "user_id": "smoke", "session_id": "clip-002"},
        timeout=600,
    )
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    return response.json(), elapsed


def query_record(
    phase: str,
    response: dict,
    seconds: float,
    client: httpx.Client,
    ollama_url: str,
) -> dict:
    return {
        "phase": phase,
        "seconds": round(seconds, 3),
        "answer": response["answer"],
        "source_files": [source["filename"] for source in response["sources"]],
        "resources_after": resource_snapshot(client, ollama_url),
        **response["metrics"],
    }


def cited_files_for_rate(answer: str, sources: list[dict], pattern: re.Pattern[str]) -> list[str]:
    cited: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer):
        if not pattern.search(sentence):
            continue
        for citation in re.findall(r"\[(\d+)\]", sentence):
            index = int(citation) - 1
            if 0 <= index < len(sources):
                cited.add(sources[index]["filename"])
    return sorted(cited)


def check_answer(answer: str, sources: list[dict], metrics: dict) -> dict:
    rate_101_sources = cited_files_for_rate(answer, sources, RATE_101)
    rate_102_sources = cited_files_for_rate(answer, sources, RATE_102)
    lowered = answer.lower()
    uncertainty_markers = (
        "приоритет не",
        "не определен",
        "не определён",
        "нельзя определить",
        "нет оснований",
        "не содержит прямой ссылки",
        "не содержат прямой ссылки",
        "не отменяет",
        "нельзя утверждать",
    )
    invented_precedence = any(
        phrase in lowered
        for phrase in (
            "приказ № 102 отменяет",
            "приказ 102 отменяет",
            "более поздний приказ имеет приоритет",
            "приказ № 102 имеет приоритет",
            "приказ 102 имеет приоритет",
            "следовательно, применяется ставка 0,05",
        )
    )
    return {
        "answer_not_empty": {"ok": bool(answer.strip()), "length": len(answer)},
        "rate_0_10_present": {"ok": bool(RATE_101.search(answer))},
        "rate_0_05_present": {"ok": bool(RATE_102.search(answer))},
        "rate_0_10_cites_order_101": {
            "ok": "order_101.txt" in rate_101_sources,
            "observed": rate_101_sources,
        },
        "rate_0_05_cites_order_102": {
            "ok": "order_102.txt" in rate_102_sources,
            "observed": rate_102_sources,
        },
        "citation_ids_valid": {"ok": metrics.get("citations_valid") is True},
        "precedence_is_uncertain": {
            "ok": any(marker in lowered for marker in uncertainty_markers)
            and not invented_precedence,
            "invented_precedence": invented_precedence,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--output", default="smoke-results-qwen.json")
    args = parser.parse_args()
    result = {
        "started_at": datetime.now().astimezone().isoformat(),
        "base_url": args.base_url,
        "ollama_url": args.ollama_url,
        "model": args.model,
        "indexing": [],
        "queries": [],
        "checks": {},
    }
    with httpx.Client() as client:
        ready = client.get(f"{args.base_url}/readyz", timeout=30)
        ready.raise_for_status()
        result["readiness"] = ready.json()
        tags = client.get(f"{args.ollama_url}/api/tags", timeout=30)
        tags.raise_for_status()
        result["model_installed"] = args.model in {
            model["name"] for model in tags.json().get("models", [])
        }

        uploaded: dict[str, dict] = {}
        for path in sorted(Path("examples").glob("*.txt")):
            started = time.perf_counter()
            with path.open("rb") as stream:
                response = client.post(
                    f"{args.base_url}/api/v1/documents",
                    files={"file": (path.name, stream, "text/plain")},
                    data={"title": path.stem.replace("_", " ")},
                    timeout=900,
                )
            elapsed = time.perf_counter() - started
            response.raise_for_status()
            uploaded[path.name] = response.json()
            result["indexing"].append({"file": path.name, "seconds": round(elapsed, 3)})

        result["model_unload"] = unload_model(client, args.ollama_url, args.model)
        result["resources_before_cold"] = resource_snapshot(client, args.ollama_url)

        cold, cold_seconds = ask(client, args.base_url)
        result["queries"].append(
            query_record("cold", cold, cold_seconds, client, args.ollama_url)
        )
        warm, warm_seconds = ask(client, args.base_url)
        result["queries"].append(
            query_record("warm", warm, warm_seconds, client, args.ollama_url)
        )

        first_titles = {source["filename"] for source in cold["sources"]}
        result["checks"]["conflict_sources_present"] = {
            "ok": {"order_101.txt", "order_102.txt"}.issubset(first_titles),
            "observed": sorted(first_titles),
        }
        result["checks"].update(check_answer(cold["answer"], cold["sources"], cold["metrics"]))

        target = uploaded["order_102.txt"]
        client.post(
            f"{args.base_url}/api/v1/documents/{target['id']}/deactivate", timeout=30
        ).raise_for_status()
        inactive, inactive_seconds = ask(client, args.base_url)
        inactive_ids = {source["document_id"] for source in inactive["sources"]}
        result["queries"].append(
            query_record("inactive", inactive, inactive_seconds, client, args.ollama_url)
        )
        result["checks"]["deactivation_excludes_document"] = {
            "ok": target["id"] not in inactive_ids,
            "document_id": target["id"],
        }

        client.post(
            f"{args.base_url}/api/v1/documents/{target['id']}/restore", timeout=30
        ).raise_for_status()
        restored, restored_seconds = ask(client, args.base_url)
        restored_ids = {source["document_id"] for source in restored["sources"]}
        result["queries"].append(
            query_record("restored", restored, restored_seconds, client, args.ollama_url)
        )
        result["checks"]["restore_returns_document"] = {
            "ok": target["id"] in restored_ids,
            "document_id": target["id"],
        }

        result["analytics"] = client.get(
            f"{args.base_url}/api/v1/admin/analytics", timeout=30
        ).json()
        traces = client.get(f"{args.base_url}/api/v1/admin/traces", timeout=30).json()
        result["checks"]["traces_recorded"] = {
            "ok": len(traces) >= 4,
            "count": len(traces),
        }

    with httpx.Client() as final_client:
        result["resources_final"] = resource_snapshot(final_client, args.ollama_url)
    result["finished_at"] = datetime.now().astimezone().isoformat()
    result["success"] = all(check["ok"] for check in result["checks"].values())
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
