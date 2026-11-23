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


def ask(client: httpx.Client, base_url: str) -> tuple[dict, float]:
    started = time.perf_counter()
    response = client.post(
        f"{base_url}/api/v1/ask",
        json={"question": QUESTION, "user_id": "smoke", "session_id": "clip-001"},
        timeout=600,
    )
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    return response.json(), elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="smoke-results.json")
    args = parser.parse_args()
    result = {
        "started_at": datetime.now().astimezone().isoformat(),
        "base_url": args.base_url,
        "indexing": [],
        "queries": [],
        "gpu_before": gpu_snapshot(),
        "ram_before_mib": round(psutil.virtual_memory().used / 1024 / 1024),
        "checks": {},
    }
    with httpx.Client() as client:
        ready = client.get(f"{args.base_url}/readyz", timeout=30)
        ready.raise_for_status()
        result["readiness"] = ready.json()
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

        first, first_seconds = ask(client, args.base_url)
        result["queries"].append({
            "phase": "first",
            "seconds": round(first_seconds, 3),
            "answer": first["answer"],
            "source_files": [source["filename"] for source in first["sources"]],
            **first["metrics"],
        })
        second, second_seconds = ask(client, args.base_url)
        result["queries"].append({
            "phase": "warm",
            "seconds": round(second_seconds, 3),
            "answer": second["answer"],
            "source_files": [source["filename"] for source in second["sources"]],
            **second["metrics"],
        })
        first_titles = {source["filename"] for source in first["sources"]}
        result["checks"]["conflict_sources_present"] = {
            "ok": {"order_101.txt", "order_102.txt"}.issubset(first_titles),
            "observed": sorted(first_titles),
        }
        result["checks"]["citations_present"] = {
            "ok": bool(re.search(r"\[\d+\]", first["answer"])),
            "observed": sorted(set(re.findall(r"\[(\d+)\]", first["answer"]))),
        }

        target = uploaded["order_102.txt"]
        client.post(f"{args.base_url}/api/v1/documents/{target['id']}/deactivate", timeout=30).raise_for_status()
        inactive, inactive_seconds = ask(client, args.base_url)
        inactive_ids = {source["document_id"] for source in inactive["sources"]}
        result["queries"].append({"phase": "inactive", "seconds": round(inactive_seconds, 3), **inactive["metrics"]})
        result["checks"]["deactivation_excludes_document"] = {
            "ok": target["id"] not in inactive_ids,
            "document_id": target["id"],
        }

        client.post(f"{args.base_url}/api/v1/documents/{target['id']}/restore", timeout=30).raise_for_status()
        restored, restored_seconds = ask(client, args.base_url)
        restored_ids = {source["document_id"] for source in restored["sources"]}
        result["queries"].append({"phase": "restored", "seconds": round(restored_seconds, 3), **restored["metrics"]})
        result["checks"]["restore_returns_document"] = {
            "ok": target["id"] in restored_ids,
            "document_id": target["id"],
        }
        result["analytics"] = client.get(f"{args.base_url}/api/v1/admin/analytics").json()
        traces = client.get(f"{args.base_url}/api/v1/admin/traces").json()
        result["checks"]["traces_recorded"] = {"ok": len(traces) >= 4, "count": len(traces)}

    result["gpu_after"] = gpu_snapshot()
    result["ram_after_mib"] = round(psutil.virtual_memory().used / 1024 / 1024)
    result["finished_at"] = datetime.now().astimezone().isoformat()
    result["success"] = all(check["ok"] for check in result["checks"].values())
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
