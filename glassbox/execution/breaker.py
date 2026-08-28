"""Circuit breaker for broker calls.

Alpaca has documented intermittent outages; the breaker stops us from hammering
a failing endpoint and turns a broker problem into a clean, logged pause rather
than a storm of retries.

    CLOSED --(N consecutive failures)--> OPEN
    OPEN --(cooldown elapsed)--> HALF_OPEN
    HALF_OPEN --(success)--> CLOSED   |   --(failure)--> OPEN
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerOpenError(Exception):
    """Raised when a call is rejected because the breaker is open."""


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ):
        import time

        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or time.monotonic
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and (
            self._clock() - self._opened_at >= self.cooldown_seconds
        ):
            self._state = BreakerState.HALF_OPEN
        return self._state

    def call(self, fn: Callable[[], T]) -> T:
        state = self.state
        if state is BreakerState.OPEN:
            remaining = self.cooldown_seconds - (self._clock() - self._opened_at)
            raise BreakerOpenError(f"circuit open, retry in {remaining:.0f}s")
        try:
            result = fn()
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result

    def _record_success(self) -> None:
        self._failures = 0
        self._state = BreakerState.CLOSED

    def _record_failure(self) -> None:
        self._failures += 1
        if self._state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()
