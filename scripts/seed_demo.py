import sys
from pathlib import Path

import httpx


base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
for path in sorted(Path("examples").glob("*.txt")):
    with path.open("rb") as stream:
        response = httpx.post(
            f"{base_url}/api/v1/documents",
            files={"file": (path.name, stream, "text/plain")},
            data={"title": path.stem.replace("_", " ")},
            timeout=900,
        )
    response.raise_for_status()
    print(path.name, response.json()["id"], response.json()["status"])
