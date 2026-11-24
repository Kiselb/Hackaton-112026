from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from clip_agent import main
from clip_agent.load_control import RequestGate


def test_gate_rejects_excess_work_without_waiting_and_recovers() -> None:
    gate = RequestGate(1, enabled=True, message="busy")

    with gate.slot():
        with pytest.raises(HTTPException) as error:
            with gate.slot():
                pass
        assert error.value.status_code == 429
        assert error.value.headers == {"Retry-After": "5"}

    with gate.slot():
        pass


def test_disabled_gate_preserves_local_unlimited_mode() -> None:
    gate = RequestGate(1, enabled=False, message="busy")

    with gate.slot(), gate.slot():
        pass


def test_upload_limit_is_configurable_and_checked_before_storage(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "demo_max_upload_mb", 1)
    upload = UploadFile(filename="large.txt", file=BytesIO(b"x" * (1024 * 1024 + 1)))

    with pytest.raises(HTTPException) as error:
        main.upload_document(upload, db=object())

    assert error.value.status_code == 413
    assert error.value.detail == "Размер файла больше 1 MB"
