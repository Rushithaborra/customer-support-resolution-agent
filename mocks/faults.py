"""Fault injection: make the next matching request(s) fail the way real networks do.

Only tests and demos queue faults (via /_admin/faults). Our app's clients never do.
"""

import threading
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException

FaultKind = Literal["error_500", "error_503", "timeout_before_apply", "timeout_after_apply", "slow"]


@dataclass
class Fault:
    kind: FaultKind
    delay_s: float
    operation: str | None  # e.g. "payment.create_refund"; None matches any operation
    remaining: int


class FaultQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: list[Fault] = []

    def add(self, fault: Fault) -> None:
        with self._lock:
            self._faults.append(fault)

    def take(self, operation: str) -> Fault | None:
        with self._lock:
            for f in self._faults:
                if f.operation in (None, operation):
                    f.remaining -= 1
                    if f.remaining <= 0:
                        self._faults.remove(f)
                    return f
        return None

    def reset(self) -> None:
        with self._lock:
            self._faults.clear()


faults = FaultQueue()


def fail_early(fault: Fault | None) -> None:
    """Faults where the operation never happens: the provider errors out,
    or the request is lost and the caller is left waiting."""
    if fault is None:
        return
    if fault.kind == "error_500":
        raise HTTPException(500, detail={"code": "internal_error", "message": "Injected 500"})
    if fault.kind == "error_503":
        raise HTTPException(503, detail={"code": "unavailable", "message": "Injected 503"},
                            headers={"Retry-After": "1"})
    if fault.kind == "timeout_before_apply":
        time.sleep(fault.delay_s)
        raise HTTPException(504, detail={"code": "dropped", "message": "Request dropped before apply"})


def delay_before_apply(fault: Fault | None) -> None:
    if fault is not None and fault.kind == "slow":
        time.sleep(fault.delay_s)


def delay_after_apply(fault: Fault | None) -> None:
    """The dangerous case: the operation is done and stored, but the response
    arrives after the caller has given up."""
    if fault is not None and fault.kind == "timeout_after_apply":
        time.sleep(fault.delay_s)
