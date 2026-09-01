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


def test_expired_entry_resolves_the_position_not_just_the_order(store, audit):
    """The 1 Sep live orphan. router.cancel marks the order canceled in the
    same tick, so it leaves orders_in_flight before the next sync can poll it
    — _on_dead never ran and the position sat at 'opening' forever, halting
    reconcile with no path back. The sweep must give it the resolution the
    poll would have."""
    from glassbox.reconcile import reconcile

    class NeverFills(StubRouter):
        def poll(self, coid):
            return "accepted", None

    router = NeverFills(store)
    t, _, _ = open_via_pipeline(store, audit, router=router)

    late = NOW + timedelta(minutes=t.cfg.execution.entry_fill_timeout_minutes + 1)
    events = lifecycle.sync(t, late)  # cancel + sweep in one pass
    assert router.cancelled
    assert any("canceled" in e for e in events), events
    assert store.open_positions() == [], "position must not stay 'opening'"
    assert store.total_heat() == 0
    assert reconcile(store, []).ok, "an orphaned entry must not poison reconcile"


def test_unconfirmed_pending_order_is_voided_after_timeout(store, audit):
    """Crash between record_order and submit: row 'pending', no
    alpaca_order_id, broker never saw the coid so every poll raises. Without
    the void this is an eternal zombie — poll errors every tick and a
    position excused as 'arriving' forever."""
    from glassbox.reconcile import reconcile

    class BrokerNeverHeardOfIt(StubRouter):
        def submit_structure(self, structure, qty, price, coid, position_id, closing=False):
            result = super().submit_structure(structure, qty, price, coid, position_id, closing)
            # regress the row to the crash state: recorded, never confirmed
            self.store.update_order(coid, status="pending", alpaca_order_id=None)
            return result

        def poll(self, coid):
            raise RuntimeError("404: order not found")

    t, _, _ = open_via_pipeline(store, audit, router=BrokerNeverHeardOfIt(store))

    assert lifecycle.sync(t, NOW) == []  # fresh: benign, retried

    late = NOW + timedelta(minutes=t.cfg.execution.entry_fill_timeout_minutes + 1)
    events = lifecycle.sync(t, late)
    assert any("voided" in e for e in events), events
    assert store.open_positions() == [], "zombie position must be failed"
    assert store.orders_in_flight() == [], "zombie order must leave in-flight"
    assert reconcile(store, []).ok


def test_confirmed_order_is_not_voided_when_polls_fail(store, audit):
    """API down is not 'the order does not exist': a row with an
    alpaca_order_id stays in flight through poll failures, however old."""

    class ApiDown(StubRouter):
        def poll(self, coid):
            raise RuntimeError("503: service unavailable")

    t, _, _ = open_via_pipeline(store, audit, router=ApiDown(store))
    late = NOW + timedelta(minutes=t.cfg.execution.entry_fill_timeout_minutes + 10)
    events = lifecycle.sync(t, late)
    assert not any("voided" in e for e in events), events
    assert len(store.orders_in_flight()) == 1
    assert store.open_positions()[0]["status"] == "opening"


def test_orphaned_closing_position_is_reopened_by_the_sweep(store, audit):
    """Same orphan class on the exit side: a close order resolved out-of-band
    leaves 'closing', which manage_positions never walks. The sweep must hand
    it back to the manager."""
    t, _router, row = open_via_pipeline(store, audit)
    settle(t)  # entry fill
    pid = row["position_id"]
    store.record_order("gbx-o-close-1", "close", [], 0.5, pid)
    store.update_order("gbx-o-close-1", status="canceled")  # died out-of-band
    store.upsert_position(pid, status="closing")

    lifecycle.sync(t, NOW + timedelta(minutes=1))
    assert store.get_position(pid)["status"] == "open", "must return to the manager"


# --- close fills -----------------------------------------------------------


def close_and_settle(store, audit):
    from glassbox.ml.bandit import ThompsonBandit

    t, router, _ = open_via_pipeline(store, audit)
    t.bandit = ThompsonBandit(store)  # make_trader wires none by default
    settle(t)  # entry fill
    t.manage_positions(NOW + timedelta(hours=1), deadline=NOW)  # forced close
    return t, router


def test_close_realises_pnl_from_the_actual_fill(store, audit):
    """The close fill arrives in Alpaca's order-oriented sign (negative =
    credit received), so realisation negates it back to entry orientation.
    Anchored by a live observation: the Friday drill's debit spread bought at
    +2.50 closed with a fill of -2.47, and the correct realised P&L is -$3 —
    not the -$497 the naive (fill - entry) formula produces."""
    t, router = close_and_settle(store, audit)
    assert store.open_positions()[0]["status"] == "closing"
    settle(t)
    closed = store.training_rows()
    assert len(closed) == 1
    entry = router.submitted[0]["price"]
    close_fill = router.submitted[1]["price"]  # stub fills at the submitted limit
    expected = (-close_fill - entry) * 100 * closed[0]["qty"]
    assert float(closed[0]["realized_pnl"]) == pytest.approx(expected)
    assert closed[0]["exit_barrier"] == "deadline"


def test_close_limit_is_order_oriented_not_entry_oriented(store, audit):
    """Closing a debit spread means SELLING it: a position currently worth
    +2.47 must be submitted with a -2.47 limit (net credit we must receive).
    The entry-signed value would tell the broker we are willing to PAY to
    exit a position that pays us — an uncontrolled market-chaser for debit
    structures and an impossible order for credit ones."""
    t, router, _ = open_via_pipeline(store, audit)
    settle(t)  # entry fills
    t.data.price = 2.47  # the spread is currently worth +2.47 to us
    t.manage_positions(NOW + timedelta(hours=1), deadline=NOW)  # forced close
    entry_order = router.submitted[0]
    close_order = router.submitted[1]
    assert not entry_order["closing"] and close_order["closing"]
    assert entry_order["price"] > 0, "debit entry pays a positive premium"
    assert close_order["price"] == pytest.approx(-2.47), "its close must demand a credit"


def test_dead_close_reopens_the_position_for_retry(store, audit):
    """A close that dies at the broker leaves orders_in_flight forever, so a
    position stuck at 'closing' would be orphaned: the manager only walks
    'open', meaning no barrier and no deadline flatten would ever touch it
    again. Death must put it back in front of the manager, and the retry must
    carry a fresh client_order_id — Alpaca never accepts a reused one."""

    class CloseDies(StubRouter):
        def poll(self, coid):
            for record in self.submitted:
                if record["coid"] == coid:
                    if record["closing"]:
                        return "expired", None  # day order died at the close
                    return "filled", record["price"]
            return "canceled", None

    router = CloseDies(store)
    t, _, _ = open_via_pipeline(store, audit, router=router)
    settle(t)  # entry fills
    t.manage_positions(NOW + timedelta(hours=1), deadline=NOW)  # submits close
    first_close = router.submitted[-1]
    events = settle(t)  # close comes back dead
    assert any("reopened" in e for e in events)
    row = store.open_positions()[0]
    assert row["status"] == "open", "a dead close must not orphan the position"

    t.manage_positions(NOW + timedelta(hours=2), deadline=NOW)  # manager retries
    second_close = router.submitted[-1]
    assert second_close["closing"]
    assert second_close["coid"] != first_close["coid"], "retry must mint a new id"


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
