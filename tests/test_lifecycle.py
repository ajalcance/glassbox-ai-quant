"""Order lifecycle tests — the seam between submitting and having.

This seam is where the review found the project's worst silent gap: nothing in
the live path confirmed fills, so positions stayed "opening" forever and the
manager — including the deadline flatten — never touched them. And the bandit
reward existed only in the drills, so live trading would have sampled untouched
priors all week. These tests are the regression fence around both.
"""

import json
from datetime import timedelta

import pytest

from glassbox.execution import lifecycle
from glassbox.ml.bandit import VolRegime
from tests.test_trader import NOW, StubRouter, make_trader, market, news, settle


def open_via_pipeline(store, audit, router=None):
    router = router or StubRouter(store)
    t = make_trader(store, audit, router=router)
    outcome = t.process_news(news(), market())
    assert outcome.traded, outcome.reason
    return t, router, store.open_positions()[0]


# --- entry fills -----------------------------------------------------------


def test_entry_fill_opens_the_position(store, audit):
    t, _router, row = open_via_pipeline(store, audit)
    assert row["status"] == "opening"
    events = settle(t)
    row = store.open_positions()[0]
    assert row["status"] == "open", "a confirmed fill must make the position managed"
    assert any("FILLED" in e for e in events)


def test_entry_fill_updates_price_and_risk_to_reality(store, audit):
    """The book carries the risk of what we actually paid, not the mid we asked
    for. A fill worse than the estimate must enlarge max_loss accordingly."""

    class WorseFill(StubRouter):
        def poll(self, coid):
            status, price = super().poll(coid)
            if status == "filled" and price is not None:
                return status, round(price * 1.2, 2)  # paid 20% over the ask
            return status, price

    t, router, row = open_via_pipeline(store, audit, router=WorseFill(store))
    estimated = float(row["max_loss"])
    settle(t)
    row = store.open_positions()[0]
    assert float(row["max_loss"]) > estimated
    assert float(row["entry_price"]) == pytest.approx(router.submitted[0]["price"] * 1.2, rel=0.01)


def test_dead_entry_frees_the_heat(store, audit):
    class Rejected(StubRouter):
        def poll(self, coid):
            return "rejected", None

    t, _, _row = open_via_pipeline(store, audit, router=Rejected(store))
    assert store.total_heat() > 0
    settle(t)
    assert store.total_heat() == 0, "a rejected entry must not keep consuming budget"
    assert store.open_positions() == []


def test_stale_entry_is_cancelled_not_chased(store, audit):
    """A news thesis is time-sensitive. An order the market has declined at our
    price for minutes is abandoned, not chased."""

    class NeverFills(StubRouter):
        def poll(self, coid):
            return "accepted", None

    router = NeverFills(store)
    t, _, _ = open_via_pipeline(store, audit, router=router)
    assert settle(t) == [] and router.cancelled == []  # fresh: left alone

    late = NOW + timedelta(minutes=t.cfg.execution.entry_fill_timeout_minutes + 1)
    events = lifecycle.sync(t, late)
    assert router.cancelled, "stale entry order was not cancelled"
    assert any("expired" in e for e in events)


# --- close fills -----------------------------------------------------------


def close_and_settle(store, audit):
    from glassbox.ml.bandit import ThompsonBandit

    t, router, _ = open_via_pipeline(store, audit)
    t.bandit = ThompsonBandit(store)  # make_trader wires none by default
    settle(t)  # entry fill
    t.manage_positions(NOW + timedelta(hours=1), deadline=NOW)  # forced close
    return t, router


def test_close_realises_pnl_from_the_actual_fill(store, audit):
    t, router = close_and_settle(store, audit)
    assert store.open_positions()[0]["status"] == "closing"
    settle(t)
    closed = store.training_rows()
    assert len(closed) == 1
    entry = router.submitted[0]["price"]
    close = router.submitted[1]["price"]
    expected = (close - entry) * 100 * closed[0]["qty"]
    assert float(closed[0]["realized_pnl"]) == pytest.approx(expected)
    assert closed[0]["exit_barrier"] == "deadline"


def test_settled_close_rewards_the_bandit(store, audit):
    """The regression that motivated this module: bandit.update existed only in
    the drills, so live trading would have sampled untouched priors all week."""
    t, _ = close_and_settle(store, audit)
    assert store.bandit_posteriors(str(VolRegime.NORMAL)) == {}
    settle(t)
    posteriors = store.bandit_posteriors(str(VolRegime.NORMAL))
    assert posteriors, "a realised close must move a posterior"
    (_alpha, _beta, pulls) = next(iter(posteriors.values()))
    assert pulls == 1


def test_close_escalates_at_a_worse_price(store, audit):
    class CloseNeverFills(StubRouter):
        def poll(self, coid):
            for record in self.submitted:
                if record["coid"] == coid:
                    if record["closing"]:
                        return "accepted", None
                    return "filled", record["price"]
            return "canceled", None

    router = CloseNeverFills(store)
    t, _, _ = open_via_pipeline(store, audit, router=router)
    settle(t)
    t.data.price = router.submitted[0]["price"]  # close prices at entry, not 0
    t.manage_positions(NOW + timedelta(hours=1), deadline=NOW)
    first_close = router.submitted[-1]

    late = NOW + timedelta(seconds=t.cfg.execution.close_retry_seconds + 5)
    events = lifecycle.sync(t, late)
    assert any("escalated" in e for e in events)
    second_close = router.submitted[-1]
    assert second_close["closing"] and second_close["coid"] != first_close["coid"]
    # Escalation concedes price toward the market, whichever sign convention.
    assert abs(second_close["price"]) != abs(first_close["price"])


def test_escalation_is_bounded(store, audit):
    class CloseNeverFills(StubRouter):
        def poll(self, coid):
            for record in self.submitted:
                if record["coid"] == coid:
                    if record["closing"]:
                        return "accepted", None
                    return "filled", record["price"]
            return "canceled", None

    router = CloseNeverFills(store)
    t, _, _ = open_via_pipeline(store, audit, router=router)
    settle(t)
    t.data.price = router.submitted[0]["price"]
    t.manage_positions(NOW + timedelta(hours=1), deadline=NOW)

    when = NOW
    for _ in range(10):
        when = when + timedelta(seconds=t.cfg.execution.close_retry_seconds + 5)
        lifecycle.sync(t, when)

    closes = [r for r in router.submitted if r["closing"]]
    assert len(closes) <= t.cfg.execution.close_max_attempts, (
        "escalation must stop; the supervisor flatten is the backstop beyond it"
    )
    exhausted = [
        json.loads(line)
        for path in sorted(audit.dir.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    assert any(r.get("kind") == "close_exhausted" for r in exhausted)
