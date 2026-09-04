"""Exit-replay tests. The integrity properties matter more than the plumbing:
a backtest that quietly extrapolates past its data is worse than none."""

import json
from datetime import UTC, date, datetime, timedelta

from glassbox.config import load_config
from glassbox.replay_exits import render, replay_exits

CFG = load_config()
DAY = date(2026, 9, 1)
OPENED = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def seed(store, audit_dir, path_pnls, *, entry=2.00, qty=1, max_loss=200.0,
         barrier="deadline", realized=10.0, kind="call_debit_spread"):
    """A closed position plus the manage ticks that trace its P&L path."""
    pid = "pos-TEST-1"
    store.upsert_position(
        pid, underlying="TEST", kind=kind,
        legs_json=json.dumps([{"symbol": "T260918C00100000", "right": "call", "strike": 100.0,
                               "expiry": "2026-09-18", "side": "long", "ratio_qty": 1}]),
        qty=qty, entry_price=entry, max_loss=max_loss, status="closed",
        horizon_hours=24.0, opened_at=OPENED.isoformat(),
        exit_barrier=barrier, realized_pnl=realized,
    )
    audit_dir.mkdir(parents=True, exist_ok=True)
    with open(audit_dir / f"{DAY}-trader.jsonl", "w") as f:
        f.writelines(json.dumps({
                "kind": "manage", "position_id": pid, "action": "hold", "barrier": "none",
                "unrealized_pnl": pnl, "ts": (OPENED + timedelta(minutes=i)).isoformat(),
            }) + "\n" for i, pnl in enumerate(path_pnls))
    return pid


def test_a_tighter_stop_fires_earlier_on_the_recorded_path(store, tmp_path):
    """The core use: the path really happened, so an earlier exit is provable."""
    audit = tmp_path / "audit"
    seed(store, audit, [0, -20, -50, -90, -30, +40])  # dips to -90 then recovers
    res = replay_exits(store, audit, [DAY], CFG)[0]
    assert res.exited_in_window
    assert res.replay_barrier == "stop"
    assert res.replay_pnl == -90, "fires at the first tick past the -$80 stop"
    assert res.replay_tick == 3


def test_a_config_that_would_hold_longer_reports_no_exit_rather_than_guessing(store, tmp_path):
    """The path ends where the position actually closed. Anything past that is
    unknowable, and inventing it is how a backtest lies."""
    audit = tmp_path / "audit"
    seed(store, audit, [0, +5, +10, +12])  # never reaches any barrier
    res = replay_exits(store, audit, [DAY], CFG)[0]
    assert not res.exited_in_window
    assert res.replay_pnl is None and res.replay_barrier == ""
    assert "no exit in window" in render([res])


def test_thesis_barriers_never_fire_without_the_spot_they_need(store, tmp_path):
    """A P&L path carries no spot, so thesis_complete must stay silent rather
    than firing on a zeroed thesis."""
    audit = tmp_path / "audit"
    seed(store, audit, [0, +5, +10], barrier="thesis_complete", realized=12.0)
    res = replay_exits(store, audit, [DAY], CFG)[0]
    assert res.replay_barrier != "thesis_complete"
    assert res.actual_needs_context, "and the real exit is flagged as unevaluable"


def test_positions_whose_real_exit_needs_context_are_excluded_from_the_total(store, tmp_path):
    audit = tmp_path / "audit"
    seed(store, audit, [0, -90], barrier="bell", realized=-50.0)
    out = render(replay_exits(store, audit, [DAY], CFG))
    assert "comparable subset (0 positions)" in out


def test_the_reconstructed_view_reproduces_the_recorded_pnl(store, tmp_path):
    """current_price is derived from the mark; a sign error here would make
    every replayed exit wrong in a way that looks plausible."""
    audit = tmp_path / "audit"
    seed(store, audit, [0, -120], entry=-1.20, qty=2, max_loss=760.0,
         kind="bull_put_spread", barrier="stop", realized=-120.0)
    res = replay_exits(store, audit, [DAY], CFG)[0]
    assert res.trough_pnl == -120
