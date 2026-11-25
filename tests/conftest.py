from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
STORAGE_ROOT = (ROOT / "storage").resolve()


def pytest_configure(config: pytest.Config) -> None:
    basetemp = config.getoption("basetemp")
    if basetemp is None:
        return

    candidate = Path(basetemp).resolve()
    if candidate == STORAGE_ROOT or STORAGE_ROOT in candidate.parents:
        raise pytest.UsageError(
            "pytest --basetemp must be outside the working STORAGE_ROOT"
        )
