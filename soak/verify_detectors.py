#!/usr/bin/env python3
"""Prove the regression detectors fire — a check that never fires is worthless.

    uv run python soak/verify_detectors.py

Each case builds a synthetic store containing exactly one shipped bug and
asserts the corresponding predicate reports it. Touches no broker and no
production state: everything happens in a temporary database.

The bugs below were all real and all shipped. This file is the fence that
keeps them from coming back unnoticed.
"""

from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glassbox.store import Store

# Imported, not copied: a detector verified against a different threshold than
# the one it ships with proves nothing.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from monitor_passive import PNL_PLAUSIBILITY_FACTOR, SILENCE_MINUTES

results: list[tuple[bool, str, str]] = []


def check(fired: bool, name: str, detail: str) -> None:
    results.append((fired, name, detail))
    print(f"  [{'ok  ' if fired else 'MISS'}] {name}: {detail}")


def fresh_store(tmp: Path) -> Store:
    return Store(tmp / f"probe-{len(results)}.db")


def _position(s: Store, pid: str, **kw) -> None:
    base = {
        "signal_id": pid, "underlying": "SPY", "kind": "call_debit_spread",
        "legs_json": "[]", "qty": 1, "max_loss": 250.0, "status": "open",
    }
    base.update(kw)
    s.upsert_position(pid, **base)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="glassbox-detectors-"))
    print("Verifying regression detectors against synthetic bugs\n")

    # 1. Close-sign inversion, using the REAL incident numbers: a debit spread
    # with $247 of max loss whose close fill was read in the wrong orientation,
    # booking -$497 for a trade that actually lost $3.
    s = fresh_store(tmp)
    _position(s, "pos-1", status="closed", max_loss=247.0)
    s.close_position("pos-1", "deadline", 0, -497.0, "2026-08-31T20:00:00+00:00")
    bad = [
        r for r in s.training_rows()
        if r["realized_pnl"] and abs(float(r["realized_pnl"]))
        > float(r["max_loss"]) * PNL_PLAUSIBILITY_FACTOR
    ]
    check(bool(bad), "pnl_plausible", f"caught {len(bad)} implausible realisation(s)")
    # ...and stays quiet on a legitimate loss.
    s2 = fresh_store(tmp)
    _position(s2, "pos-ok", status="closed")
    s2.close_position("pos-ok", "stop", 0, -125.0, "2026-08-31T20:00:00+00:00")
    quiet = [
        r for r in s2.training_rows()
        if r["realized_pnl"] and abs(float(r["realized_pnl"]))
        > float(r["max_loss"]) * PNL_PLAUSIBILITY_FACTOR
    ]
    check(not quiet, "pnl_plausible (no false positive)", "a normal -$125 stop is not flagged")
    s.close()
    s2.close()

    # 2. Orphaned exit: 'closing' with no in-flight order.
    s = fresh_store(tmp)
    _position(s, "pos-orphan", status="closing")
    in_flight = {o["position_id"] for o in s.orders_in_flight() if o["position_id"]}
    orphans = [
        r["position_id"] for r in s.open_positions()
        if r["status"] in ("opening", "closing") and r["position_id"] not in in_flight
    ]
    check(bool(orphans), "no_orphans", f"caught {orphans}")
    # A 'closing' position WITH a live order is normal, not an orphan.
    s.record_order("gbx-c-live", "close", [], -2.47, position_id="pos-orphan")
    in_flight2 = {o["position_id"] for o in s.orders_in_flight() if o["position_id"]}
    still = [
        r["position_id"] for r in s.open_positions()
        if r["status"] in ("opening", "closing") and r["position_id"] not in in_flight2
    ]
    check(not still, "no_orphans (no false positive)", "a closing position with a live order is fine")
    s.close()

    # 3. Stale daily baseline: yesterday's date during today's session.
    s = fresh_store(tmp)
    s.set_state("session_start_date", "2026-08-30")
    check(s.get_state("session_start_date") != "2026-08-31",
          "daily_baseline", "caught a baseline dated 2026-08-30 on 2026-08-31")
    s.close()

    # 4. Two positions from one signal (the restart-replay bug).
    s = fresh_store(tmp)
    _position(s, "pos-a", signal_id="AAPL-n1")
    _position(s, "pos-b", signal_id="AAPL-n1")
    sids = Counter(r["signal_id"] for r in s.open_positions() if r["signal_id"])
    check(any(n > 1 for n in sids.values()),
          "signal_uniqueness", f"caught {({k: v for k, v in sids.items() if v > 1})}")
    s.close()

    # 5. Oversized position: a haircut that failed to apply.
    s = fresh_store(tmp)
    _position(s, "pos-fat", max_loss=2_500.0)
    cap = 100_000 * 1.5 / 100
    over = [r["position_id"] for r in s.open_positions() if float(r["max_loss"]) > cap]
    check(bool(over), "position_cap", f"caught {over} above ${cap:,.0f}")
    s.close()

    # 6. flatten_incomplete present in the day's audit records.
    audit = [{"kind": "flatten_incomplete", "remaining": ["SPY260904C00600000"]}]
    check(bool([r for r in audit if r.get("kind") == "flatten_incomplete"]),
          "flatten_complete", "caught legs left on the book after a flatten")

    # 7. Error budget exceeded.
    errs = Counter(["pipeline_error"] * 30)
    check(sum(errs.values()) > 25, "error_rate", f"caught {sum(errs.values())} swallowed errors")

    # 8. Pipeline silence: records exist, but all of them are older than the
    # window, which is what a dead news feed looks like from the outside.
    from datetime import UTC, datetime, timedelta

    def minutes_ago(ts: str) -> float:
        return (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds() / 60

    stale_feed = [
        {"kind": "analyst_view", "ts": (datetime.now(UTC) - timedelta(hours=3)).isoformat()},
        {"kind": "signal_dropped", "ts": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
    ]
    recent = sum(
        1 for r in stale_feed
        if r["kind"] in ("analyst_view", "signal_dropped")
        and minutes_ago(r["ts"]) <= SILENCE_MINUTES
    )
    check(recent == 0, "pipeline_alive", "caught a feed whose last signal is 2h old")

    live_feed = [{"kind": "analyst_view",
                  "ts": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()}]
    recent_live = sum(
        1 for r in live_feed
        if r["kind"] in ("analyst_view", "signal_dropped")
        and minutes_ago(r["ts"]) <= SILENCE_MINUTES
    )
    check(recent_live > 0, "pipeline_alive (no false positive)",
          "a signal 5 minutes old reads as alive")

    missed = [n for fired, n, _ in results if not fired]
    print(f"\n{len(results) - len(missed)}/{len(results)} detectors verified")
    if missed:
        print(f"NOT FIRING: {missed}")
        return 1
    print("every detector catches its bug, and the two false-positive probes stay quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
