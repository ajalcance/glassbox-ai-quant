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
