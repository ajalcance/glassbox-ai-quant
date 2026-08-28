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
