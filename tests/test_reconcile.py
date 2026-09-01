import json

from glassbox.reconcile import enforce, is_halted, reconcile


class FakeBrokerPos:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty


def add_position(store, position_id, legs, qty=1, status="open"):
    store.upsert_position(
        position_id,
        underlying="SPY",
        kind="bull_put_spread",
        legs_json=json.dumps(legs),
        qty=qty,
        max_loss=380.0,
        status=status,
    )


SHORT_PUT = {"symbol": "SPY260918P00440000", "side": "short", "ratio_qty": 1}
LONG_PUT = {"symbol": "SPY260918P00435000", "side": "long", "ratio_qty": 1}


def test_in_sync(store, audit):
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT])
    broker = [FakeBrokerPos("SPY260918P00440000", -1), FakeBrokerPos("SPY260918P00435000", 1)]
    result = reconcile(store, broker)
    assert result.ok and result.reason == "in sync"


def test_broker_only_position_halts(store, audit):
    """Broker holds something we have no record of — the dangerous case."""
    broker = [FakeBrokerPos("SPY260918C00460000", -1)]
    result = enforce(store, audit, broker)
    assert not result.ok
    assert "broker-only" in result.reason
    assert is_halted(store)


def test_local_only_position_halts(store, audit):
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT])
    result = enforce(store, audit, broker_positions=[])
    assert not result.ok
    assert "local-only" in result.reason
    assert is_halted(store)


def test_quantity_mismatch_halts(store, audit):
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT], qty=1)
    broker = [FakeBrokerPos("SPY260918P00440000", -2), FakeBrokerPos("SPY260918P00435000", 1)]
    result = enforce(store, audit, broker)
    assert not result.ok
    assert "qty mismatch" in result.reason
    assert is_halted(store)


def test_multi_spread_quantities_scale(store, audit):
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT], qty=3)
    broker = [FakeBrokerPos("SPY260918P00440000", -3), FakeBrokerPos("SPY260918P00435000", 3)]
    assert reconcile(store, broker).ok


def test_halt_clears_when_divergence_resolves(store, audit):
    enforce(store, audit, [FakeBrokerPos("SPY260918C00460000", -1)])
    assert is_halted(store)
    enforce(store, audit, [])  # broker position closed out
    assert not is_halted(store)


def test_resting_order_alone_does_not_halt(store, audit):
    """An unfilled order is normal; only position divergence halts."""
    store.record_order("gbx-o-1", "open", [SHORT_PUT, LONG_PUT], -1.20, "pos-1")
    result = enforce(store, audit, broker_positions=[])
    assert result.ok
    assert result.in_flight == ("gbx-o-1",)
    assert not is_halted(store)


def test_opening_position_with_working_entry_order_does_not_halt(store, audit):
    """The 1 Sep live halt: entry submitted, order resting NEW at the broker,
    position row 'opening'. The legs are expected to be absent — that is what
    'opening' means. record_order + upsert_position together is what the
    trader actually writes; testing the order row alone is what let this
    through 400 tests."""
    store.record_order("gbx-o-1", "open", [SHORT_PUT, LONG_PUT], -0.91, "pos-1")
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT], qty=2, status="opening")
    result = enforce(store, audit, broker_positions=[])
    assert result.ok, result.reason
    assert not is_halted(store)


def test_opening_position_partially_filled_within_band_does_not_halt(store, audit):
    """1 of 2 spreads filled while the order still works: broker quantities sit
    between nothing-filled and fully-filled, which is legal mid-flight."""
    store.record_order("gbx-o-1", "open", [SHORT_PUT, LONG_PUT], -0.91, "pos-1")
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT], qty=2, status="opening")
    broker = [FakeBrokerPos("SPY260918P00440000", -1), FakeBrokerPos("SPY260918P00435000", 1)]
    result = enforce(store, audit, broker)
    assert result.ok, result.reason


def test_opening_position_overfilled_beyond_band_halts(store, audit):
    """More at the broker than the working order could ever deliver is a real
    divergence, opening status or not."""
    store.record_order("gbx-o-1", "open", [SHORT_PUT, LONG_PUT], -0.91, "pos-1")
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT], qty=2, status="opening")
    broker = [FakeBrokerPos("SPY260918P00440000", -3), FakeBrokerPos("SPY260918P00435000", 3)]
    result = enforce(store, audit, broker)
    assert not result.ok
    assert "qty mismatch" in result.reason


def test_opening_position_whose_order_vanished_halts(store, audit):
    """'opening' is only excused while its entry order is actually working.
    Order canceled with the row still 'opening' is unexplained state."""
    store.record_order("gbx-o-1", "open", [SHORT_PUT, LONG_PUT], -0.91, "pos-1")
    store.update_order("gbx-o-1", status="canceled")
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT], qty=2, status="opening")
    result = enforce(store, audit, broker_positions=[])
    assert not result.ok
    assert "local-only" in result.reason
    assert is_halted(store)


def test_broker_only_still_halts_alongside_pending_entry(store, audit):
    """A pending entry excuses ITS legs only — an unrelated broker position is
    still the dangerous case."""
    store.record_order("gbx-o-1", "open", [SHORT_PUT, LONG_PUT], -0.91, "pos-1")
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT], qty=2, status="opening")
    broker = [FakeBrokerPos("SPY260918C00460000", -1)]
    result = enforce(store, audit, broker)
    assert not result.ok
    assert "broker-only" in result.reason


def test_closed_positions_excluded_from_heat_and_reconcile(store, audit):
    add_position(store, "pos-old", [SHORT_PUT, LONG_PUT], status="closed")
    assert reconcile(store, []).ok
    assert store.total_heat() == 0.0


def test_heat_sums_open_positions(store, audit):
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT])
    add_position(store, "pos-2", [SHORT_PUT, LONG_PUT])
    assert store.total_heat() == 760.0


# --- position lifecycle ---------------------------------------------------


def test_peak_pnl_ratchets_up_only(store):
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT])
    assert store.record_peak_pnl("pos-1", 40.0) == 40.0
    assert store.record_peak_pnl("pos-1", 55.0) == 55.0
    assert store.record_peak_pnl("pos-1", 20.0) == 55.0, "peak must not decay"


def test_closing_records_barrier_and_label(store):
    add_position(store, "pos-1", [SHORT_PUT, LONG_PUT])
    store.close_position("pos-1", "profit", 1, 60.0, "2026-09-02T15:00:00+00:00")
    rows = store.training_rows()
    assert len(rows) == 1
    assert rows[0]["exit_barrier"] == "profit" and rows[0]["meta_label"] == 1
    assert store.open_positions() == [], "closed position must leave the open set"
    assert store.total_heat() == 0.0, "closed position must release its heat"


def test_unlabelled_positions_excluded_from_training_set(store):
    add_position(store, "pos-open", [SHORT_PUT, LONG_PUT])
    assert store.training_rows() == []


def test_migration_adds_columns_to_an_older_database(tmp_path):
    """A store created by an earlier version must not fail mid-session."""
    import sqlite3

    from glassbox.store import Store

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE positions (position_id TEXT PRIMARY KEY, signal_id TEXT, "
        "underlying TEXT NOT NULL, kind TEXT NOT NULL, legs_json TEXT NOT NULL, "
        "qty INTEGER NOT NULL, entry_price REAL, max_loss REAL NOT NULL, "
        "status TEXT NOT NULL, opened_at TEXT, closed_at TEXT, exit_reason TEXT, "
        "realized_pnl REAL);"
    )
    conn.commit()
    conn.close()

    s = Store(path)  # must migrate rather than explode
    cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(positions)")}
    assert {"peak_pnl", "exit_barrier", "meta_label", "horizon_hours"} <= cols
    s.close()


# --- assignment detection -------------------------------------------------


class FakeActivity:
    def __init__(self, symbol, qty="1", day="2026-09-02", assignment=True):
        self.symbol = symbol
        self.qty = qty
        self.date = day
        self.is_assignment = assignment

    def __str__(self):
        return f"{self.symbol} {'assigned' if self.is_assignment else 'expired'}"


def test_assignment_halts_trading(store, audit):
    """An assignment is invisible to position reconciliation: the option is gone
    from both sides, so both agree — while we now hold stock the gate never
    approved."""
    from glassbox.reconcile import check_assignments

    assigned = check_assignments(store, audit, [FakeActivity("AAPL260918C00230000")])
    assert assigned == ("AAPL260918C00230000",)
    assert is_halted(store)
    assert "assignment" in store.get_state("halt_reason")


def test_expiration_does_not_halt(store, audit):
    """A position expiring worthless is the normal end of a trade."""
    from glassbox.reconcile import check_assignments

    assert (
        check_assignments(store, audit, [FakeActivity("SPY260903P00420000", assignment=False)])
        == ()
    )
    assert not is_halted(store)


def test_same_assignment_is_not_re_reported(store, audit):
    """The supervisor polls every 15s; one event must halt once, not forever."""
    from glassbox.reconcile import check_assignments

    activity = FakeActivity("AAPL260918C00230000")
    assert check_assignments(store, audit, [activity]) == ("AAPL260918C00230000",)
    assert check_assignments(store, audit, [activity]) == ()


def test_a_second_distinct_assignment_is_reported(store, audit):
    from glassbox.reconcile import check_assignments

    check_assignments(store, audit, [FakeActivity("AAPL260918C00230000")])
    fresh = check_assignments(store, audit, [FakeActivity("MSFT260918C00400000")])
    assert fresh == ("MSFT260918C00400000",)


def test_no_activities_is_a_no_op(store, audit):
    from glassbox.reconcile import check_assignments

    assert check_assignments(store, audit, []) == ()
    assert not is_halted(store)


# --- daily position counting ----------------------------------------------


def test_positions_opened_today_excludes_carried_positions(store):
    """The rate limit counts positions *opened today*. Carrying three overnight
    would otherwise consume three of the day's ten slots without a new order."""
    store.upsert_position(
        "pos-yesterday",
        underlying="SPY",
        kind="bull_put_spread",
        legs_json="[]",
        qty=1,
        max_loss=100.0,
        status="open",
        opened_at="2026-08-31T15:00:00+00:00",
    )
    store.upsert_position(
        "pos-today",
        underlying="QQQ",
        kind="bull_put_spread",
        legs_json="[]",
        qty=1,
        max_loss=100.0,
        status="open",
        opened_at="2026-09-01T15:00:00+00:00",
    )
    assert store.positions_opened_on("2026-09-01") == 1
    assert store.positions_opened_on("2026-08-31") == 1


def test_positions_opened_today_includes_ones_already_closed(store):
    """A position opened and closed within the day still used a slot."""
    store.upsert_position(
        "pos-roundtrip",
        underlying="SPY",
        kind="bull_put_spread",
        legs_json="[]",
        qty=1,
        max_loss=100.0,
        status="closed",
        opened_at="2026-09-01T14:00:00+00:00",
    )
    assert store.positions_opened_on("2026-09-01") == 1
    assert store.open_positions() == []
