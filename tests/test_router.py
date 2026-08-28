"""Router tests, including the chaos cases: broker timeouts, duplicate
submissions, and the invariant that a naked structure can never reach the wire.
"""
import pytest

from glassbox.execution.breaker import BreakerOpenError, CircuitBreaker
from glassbox.execution.ids import client_order_id, close_order_id
from glassbox.execution.router import OrderRouter, PriceLadder
from glassbox.structures import UndefinedRiskError, structure_key


class FakeOrder:
    def __init__(self, oid="alp-1"):
        self.id = oid


class FakeClient:
    """Records submissions; can be told to fail a number of times first."""

    def __init__(self, fail_times=0, exc=ConnectionError("alpaca 503")):
        self.submitted = []
        self.canceled = []
        self.fail_times = fail_times
        self.exc = exc

    def submit_order(self, request):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        self.submitted.append(request)
        return FakeOrder(f"alp-{len(self.submitted)}")

    def cancel_order_by_id(self, oid):
        self.canceled.append(oid)


def make_router(store, audit, **kw):
    return OrderRouter(FakeClient(**kw), store, audit, breaker=CircuitBreaker(clock=lambda: 0.0))


# ---- idempotency ---------------------------------------------------------

def test_ids_are_deterministic_and_distinct():
    a = client_order_id("sig-1", "SPY|bull_put|x", 0)
    assert a == client_order_id("sig-1", "SPY|bull_put|x", 0)
    assert a != client_order_id("sig-1", "SPY|bull_put|x", 1)      # retry differs
    assert a != client_order_id("sig-2", "SPY|bull_put|x", 0)      # signal differs
    # a stop-loss close and a time-stop close are different orders
    assert close_order_id("p1", "stop") != close_order_id("p1", "time")
    assert close_order_id("p1", "stop") == close_order_id("p1", "stop")


def test_duplicate_submission_is_suppressed(store, audit, bull_put):
    router = make_router(store, audit)
    coid = client_order_id("sig-1", structure_key(bull_put))
    router.submit_structure(bull_put, 1, -1.20, coid, "pos-1")
    assert len(router.client.submitted) == 1

    # Simulate a retry after a timeout: same id, must not double-fill.
    router.submit_structure(bull_put, 1, -1.20, coid, "pos-1")
    assert len(router.client.submitted) == 1, "duplicate order reached the broker"


def test_intent_persisted_before_submission(store, audit, bull_put):
    """A crash mid-submit must still leave a reconcilable record."""
    router = make_router(store, audit, fail_times=1)
    coid = client_order_id("sig-1", structure_key(bull_put))
    with pytest.raises(ConnectionError):
        router.submit_structure(bull_put, 1, -1.20, coid, "pos-1")
    row = store.get_order(coid)
    assert row is not None, "no order record survived the failure"
    assert row["status"] == "rejected"


# ---- safety invariants ---------------------------------------------------

def test_naked_structure_never_reaches_broker(store, audit, naked_put):
    router = make_router(store, audit)
    with pytest.raises(UndefinedRiskError):
        router.submit_structure(naked_put, 1, -1.0, "gbx-o-x", "pos-1")
    assert router.client.submitted == [], "undefined-risk order reached the broker"


def test_zero_qty_rejected(store, audit, bull_put):
    router = make_router(store, audit)
    with pytest.raises(ValueError):
        router.submit_structure(bull_put, 0, -1.20, "gbx-o-y", "pos-1")
    assert router.client.submitted == []


def test_order_is_multileg_limit_never_market(store, audit, bull_put):
    router = make_router(store, audit)
    router.submit_structure(bull_put, 2, -1.20, "gbx-o-z", "pos-1")
    req = router.client.submitted[0]
    assert req.order_class.value == "mleg"
    assert req.type.value == "limit"
    assert req.limit_price == -1.20
    assert req.qty == 2
    assert len(req.legs) == 2
    assert {l.side.value for l in req.legs} == {"buy", "sell"}


def test_closing_order_inverts_every_leg(store, audit, bull_put):
    router = make_router(store, audit)
    router.submit_structure(bull_put, 1, 0.60, "gbx-c-1", "pos-1", closing=True)
    intents = {l.position_intent.value for l in router.client.submitted[0].legs}
    assert intents == {"buy_to_close", "sell_to_close"}


# ---- chaos ---------------------------------------------------------------

def test_repeated_broker_failure_opens_breaker(store, audit, bull_put):
    router = OrderRouter(
        FakeClient(fail_times=99), store, audit,
        breaker=CircuitBreaker(failure_threshold=3, clock=lambda: 0.0),
    )
    for i in range(3):
        with pytest.raises(ConnectionError):
            router.submit_structure(bull_put, 1, -1.20, f"gbx-o-{i}", f"pos-{i}")
    # Fourth attempt is refused locally rather than hammering a sick endpoint.
    with pytest.raises(BreakerOpenError):
        router.submit_structure(bull_put, 1, -1.20, "gbx-o-3", "pos-3")


# ---- price ladder --------------------------------------------------------

def test_ladder_walks_debit_up_and_credit_down():
    debit = PriceLadder(start=2.00, tick=0.05, max_steps=3, is_debit=True)
    assert [debit.price_at(i) for i in range(4)] == [2.00, 2.05, 2.10, 2.15]
    credit = PriceLadder(start=1.20, tick=0.05, max_steps=3, is_debit=False)
    assert [credit.price_at(i) for i in range(4)] == [1.20, 1.15, 1.10, 1.05]


def test_ladder_is_bounded():
    """We never chase further than max_steps — bounds the edge we give up."""
    ladder = PriceLadder(start=2.00, tick=0.05, max_steps=2, is_debit=True)
    assert ladder.price_at(99) == ladder.price_at(2) == 2.10
