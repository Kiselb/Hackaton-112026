from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore

from fastapi import HTTPException


class RequestGate:
    """Reject excess work immediately instead of creating an unbounded queue."""

    def __init__(self, capacity: int, *, enabled: bool, message: str) -> None:
        self._semaphore = BoundedSemaphore(capacity)
        self._enabled = enabled
        self._message = message

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._enabled:
            yield
            return
        acquired = self._semaphore.acquire(blocking=False)
        if not acquired:
            raise HTTPException(
                status_code=429,
                detail=self._message,
                headers={"Retry-After": "5"},
            )
        try:
            yield
        finally:
            self._semaphore.release()


def slot_dependency(gate: RequestGate) -> Iterator[None]:
    with gate.slot():
        yield
