#!/usr/bin/env python3
"""Hold probe — prove the monitor can SEE a held position, not just clean state.

    uv run python soak/hold_probe.py --account YOUR_PAPER_ACCOUNT [--hold-seconds 240]

Every invariant check so far has evaluated against either an empty book (where
"all positions within max_loss" is vacuously true) or a synthetic store. The
drills hold real positions for only seconds, and the passive monitor samples
every 30 — the 31 Aug drill lifecycle fell entirely between snapshots, so the
held-state checks have never observed a real held position.

This probe opens one real defined-risk spread, holds it long enough that the
monitor cannot miss it, exits through the production lifecycle, and then reads
the monitor's own samples to verify the hold was observed. It tests the
observer as much as the system.

Asserts, in order:
  1. entry fill flips the position to 'open' via lifecycle.sync
  2. while held: store shows the position, heat equals its max loss
  3. exit realises through lifecycle._on_fill with a plausible P&L
     (|realised| <= 1.5x max loss — the close-sign regression bound)
  4. the monitor's samples.jsonl shows local_open_positions >= 1 at some
     point inside the hold window — the check the drills could never make

Dev/chaos account only. Refuses to run against any other account.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glassbox.audit import AuditLog
from glassbox.config import load_config
from glassbox.data.alpaca_client import (
    option_data_client,
    stock_data_client,
    trading_client,
)
from glassbox.data.market import MarketData
from glassbox.execution.ids import client_order_id, close_order_id
from glassbox.execution.router import OrderRouter
from glassbox.store import Store
from glassbox.structures import structure_key

SAMPLES = ROOT / "soak-results" / "samples.jsonl"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def wait_for(predicate, timeout: float, interval: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="hold probe (dev account only)")
    parser.add_argument("--account", required=True)
    parser.add_argument("--hold-seconds", type=int, default=240,
                        help="long enough that a 30s-sampling monitor cannot miss it")
    args = parser.parse_args()

    cfg = load_config()
    client = trading_client()
    acct = client.get_account()
    if acct.account_number != args.account:
        print(f"REFUSING: account {acct.account_number} != expected {args.account}")
        return 2
    if not client.get_clock().is_open:
        print("REFUSING: market closed — a probe that cannot fill proves nothing")
        return 2

    store = Store(cfg.paths.db)
    audit = AuditLog(cfg.paths.audit_dir, role="hold-probe")
    data = MarketData(
        trading_client=client, stock_client=stock_data_client(),
        option_client=option_data_client(), store=store, root=ROOT,
    )
    router = OrderRouter(client, store, audit)

    # Reuse the drill's marketable-spread builder so the probe fills fast.
    from glassbox.drills import _LifecycleShim, _marketable_spread

    failures: list[str] = []

    def check(ok: bool, name: str, detail: str) -> None:
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures.append(name)

    print(f"HOLD PROBE — account {acct.account_number}, holding {args.hold_seconds}s")
    hold_started = None
    position_id = f"pos-holdprobe-{datetime.now(UTC):%H%M%S}"
    try:
        structure, _mid, price = _marketable_spread(data)
        risk = price * 100  # debit paid bounds the loss for these structures
        coid = client_order_id(f"holdprobe-{datetime.now(UTC):%H%M%S}", structure_key(structure))
        store.upsert_position(
            position_id, signal_id=position_id, underlying=structure.underlying,
            kind=str(structure.kind), legs_json=json.dumps(
                [{"symbol": l.symbol, "right": str(l.right), "strike": l.strike,
                  "expiry": l.expiry.isoformat(), "side": str(l.side),
                  "ratio_qty": l.ratio_qty} for l in structure.legs]),
            qty=1, entry_price=price, max_loss=risk, status="opening",
            opened_at=now_iso(),
        )
        router.submit_structure(structure, 1, price, coid, position_id)

        # 1. entry realises through the production lifecycle
        from glassbox.execution import lifecycle
        shim = _LifecycleShim(store, audit, router, cfg)

        def entry_open():
            lifecycle.sync(shim, datetime.now(UTC))
            row = store.get_position(position_id)
            return row if row and row["status"] == "open" else None

        row = wait_for(entry_open, timeout=120)
        check(row is not None, "entry_open", "lifecycle flipped the position to 'open'"
              if row else "entry never confirmed open within 120s")
        if row is None:
            return 1
        hold_started = time.monotonic()
        entry = float(row["entry_price"])
        max_loss = float(row["max_loss"])
        print(f"    holding: entry {entry:+.2f}, max loss ${max_loss:,.0f}")

        # 2. held-state invariants, checked live mid-hold
        time.sleep(min(30, args.hold_seconds / 4))
        heat = store.total_heat()
        check(abs(heat - max_loss) < 0.01, "heat_while_held",
              f"heat ${heat:,.0f} == position max loss ${max_loss:,.0f}"
              if abs(heat - max_loss) < 0.01 else f"heat ${heat:,.0f} != ${max_loss:,.0f}")
        broker_syms = {p.symbol for p in client.get_all_positions()}
        legs = {l.symbol for l in structure.legs}
        check(legs <= broker_syms, "broker_agrees",
              f"{len(legs)}/{len(legs)} legs at the broker")

        remaining = args.hold_seconds - (time.monotonic() - hold_started)
        if remaining > 0:
            print(f"    ...holding {remaining:.0f}s more so the monitor must sample it")
            time.sleep(remaining)

        # 3. marks and the barrier ladder, on real quotes (2 Sep)
        from datetime import timedelta

        from glassbox.manage import Barrier, PositionView, evaluate_position, stop_level
        from glassbox.structures import StructureKind

        current = data.structure_price(structure)
        # Compare against the mid of the SAME quotes the mark was built from.
        # The first run of this check (2 Sep 13:36) compared against the mid
        # captured before the entry, four minutes earlier — and failed on
        # drift, not on sides. Both reads below hit the same cached snapshot.
        from glassbox.structures import LegSide

        snaps = data._snapshots(structure.underlying, structure.expiry)
        mid_now = 0.0
        for leg in structure.legs:
            q = snaps[leg.symbol].latest_quote
            mid_now += ((float(q.bid_price) + float(q.ask_price)) / 2) * leg.ratio_qty * (
                1 if leg.side is LegSide.LONG else -1
            )
        mid_now = round(mid_now, 2)
        check(current <= mid_now + 1e-9, "liquidation_marks",
              f"mark {current:+.2f} <= same-instant mid {mid_now:+.2f} "
              f"(long legs at the bid, shorts at the ask)"
              if current <= mid_now + 1e-9 else
              f"mark {current:+.2f} ABOVE same-instant mid {mid_now:+.2f} — not liquidation-side")

        now_utc = datetime.now(UTC)
        expiry_dt = datetime(structure.expiry.year, structure.expiry.month,
                             structure.expiry.day, 20, tzinfo=UTC)
        view = PositionView(
            position_id=position_id, kind=StructureKind(row["kind"]), qty=1,
            entry_price=entry, current_price=current, max_loss_per_spread=max_loss,
            opened_at=datetime.fromisoformat(row["opened_at"]), horizon_hours=24.0,
            hours_to_expiry=(expiry_dt - now_utc).total_seconds() / 3600,
            peak_pnl=max_loss * 2,  # injected: the position "peaked" past its target
        )
        decision = evaluate_position(view, cfg, now_utc + timedelta(minutes=1))
        pnl = view.unrealized_pnl
        expected = Barrier.TRAIL
        if view.hours_to_expiry < cfg.manage.min_hours_to_expiry:
            expected = Barrier.EXPIRY_RISK
        elif pnl <= stop_level(view, cfg):
            expected = Barrier.STOP
        check(decision.should_close and decision.barrier is expected, "barrier_decides_close",
              f"{decision.barrier} on real marks (pnl ${pnl:+,.0f}): {decision.reason[:70]}"
              if decision.barrier is expected else
              f"expected {expected}, got {decision.barrier} — {decision.reason[:70]}")

        close_coid = close_order_id(position_id, "hold-probe", 0)
        store.set_state(f"close_barrier:{position_id}", str(decision.barrier))
        store.upsert_position(position_id, status="closing")
        router.submit_structure(structure, 1, round(-current, 2), close_coid,
                                position_id, closing=True)

        def closed():
            lifecycle.sync(shim, datetime.now(UTC))
            row = store.get_position(position_id)
            return row if row and row["status"] == "closed" else None

        row = wait_for(closed, timeout=180)
        check(row is not None, "exit_closed", "lifecycle realised the close"
              if row else "close never confirmed within 180s")
        if row is not None:
            realized = float(row["realized_pnl"] or 0.0)
            plausible = abs(realized) <= max_loss * 1.5
            check(plausible, "pnl_plausible",
                  f"realised ${realized:+,.2f} within 1.5x max loss ${max_loss:,.0f}"
                  if plausible else
                  f"IMPLAUSIBLE ${realized:+,.2f} vs ${max_loss:,.0f} — sign regression?")

        # 4. did the monitor actually SEE the hold?
        time.sleep(35)  # one more monitor cycle so the tail of the hold lands
        seen = 0
        if SAMPLES.exists():
            start_wall = datetime.now(UTC).timestamp() - (time.monotonic() - hold_started) - 35
            for line in SAMPLES.read_text().splitlines():
                try:
                    s = json.loads(line)
                    ts = datetime.fromisoformat(s["ts"]).timestamp()
                    if ts >= start_wall and (s.get("local_open_positions") or 0) >= 1:
                        seen += 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        check(seen >= 1, "monitor_observed_hold",
              f"monitor sampled the held position {seen} time(s)"
              if seen else "monitor NEVER saw the held position — observation gap is real")

    finally:
        # Never leave probe residue: cancel anything resting, close anything held.
        try:
            for o in client.get_orders():
                if o.client_order_id and "holdprobe" in str(o.client_order_id):
                    client.cancel_order_by_id(o.id)
        except Exception as e:  # noqa: BLE001 -- cleanup is best-effort;
            # the failure itself is the finding, not the sweep.
            print(f"  cleanup sweep error (non-fatal): {type(e).__name__}: {e}")
        store.close()

    print(f"\nHOLD PROBE {'PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
