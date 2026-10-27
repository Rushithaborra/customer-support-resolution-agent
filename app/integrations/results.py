"""Every external call returns one of four outcomes. Callers must handle each;
nothing is hidden behind an exception or an automatic retry.

  Ok             the call succeeded; value is the parsed response
  Rejected       the provider said no (4xx). Retrying the same request won't help.
  Retryable      nothing happened (refused, 503, failed read). Safe to try again later.
  UnknownOutcome a write may or may not have been applied (timeout, 5xx, dropped
                 connection). Reconcile with the provider before any retry.
"""

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T
    replayed: bool = False  # provider returned a stored response for a reused idempotency key


@dataclass(frozen=True)
class Rejected:
    status_code: int
    code: str
    message: str


@dataclass(frozen=True)
class Retryable:
    reason: str


@dataclass(frozen=True)
class UnknownOutcome:
    reason: str


Result = Ok[T] | Rejected | Retryable | UnknownOutcome
