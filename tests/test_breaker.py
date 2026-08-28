import pytest

from glassbox.execution.breaker import BreakerOpenError, BreakerState, CircuitBreaker


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def boom():
    raise ConnectionError("alpaca 503")


def test_opens_after_threshold_failures():
    b = CircuitBreaker(failure_threshold=3, clock=FakeClock())
    for _ in range(3):
        with pytest.raises(ConnectionError):
            b.call(boom)
    assert b.state is BreakerState.OPEN
    with pytest.raises(BreakerOpenError):
        b.call(lambda: "should not run")


def test_success_resets_failure_count():
    b = CircuitBreaker(failure_threshold=3, clock=FakeClock())
    for _ in range(2):
        with pytest.raises(ConnectionError):
            b.call(boom)
    assert b.call(lambda: "ok") == "ok"
    with pytest.raises(ConnectionError):
        b.call(boom)
    assert b.state is BreakerState.CLOSED  # count was reset, not at threshold


def test_half_open_after_cooldown_then_closes_on_success():
    clock = FakeClock()
    b = CircuitBreaker(failure_threshold=2, cooldown_seconds=60, clock=clock)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            b.call(boom)
    assert b.state is BreakerState.OPEN
    clock.advance(61)
    assert b.state is BreakerState.HALF_OPEN
    assert b.call(lambda: "recovered") == "recovered"
    assert b.state is BreakerState.CLOSED


def test_half_open_failure_reopens_immediately():
    clock = FakeClock()
    b = CircuitBreaker(failure_threshold=2, cooldown_seconds=60, clock=clock)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            b.call(boom)
    clock.advance(61)
    assert b.state is BreakerState.HALF_OPEN
    with pytest.raises(ConnectionError):
        b.call(boom)
    assert b.state is BreakerState.OPEN  # one probe failure is enough
