"""Supervisor entrypoint — a separate process from the trader.

    uv run python -m glassbox.supervisor.run

It holds its own Alpaca client and connection pool, polls independently, and
never imports trader state. That separation is the point: a trader stuck in a
retry loop, deadlocked on a lock, or blocked on a hung socket cannot prevent
this process from flattening the book.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from glassbox.audit import AuditLog
from glassbox.clock import now_utc
from glassbox.config import load_config
from glassbox.data.alpaca_client import supervisor_trading_client
from glassbox.reconcile import HALT_KEY
from glassbox.store import Store
from glassbox.supervisor.guards import (
    KILL_SWITCH_FILE,
    SESSION_START_EQUITY_KEY,
    GuardAction,
    broker_peak_equity,
    evaluate_guards,
    update_peak_equity,
)

HEARTBEAT_KEY = "trader_heartbeat"


def kill_switch_engaged(root: Path) -> bool:
    return (root / KILL_SWITCH_FILE).exists()


def heartbeat_age(store) -> float:
    stamp = store.get_state(HEARTBEAT_KEY)
    if not stamp:
        return 0.0  # trader has never run; nothing to supervise yet
    from datetime import datetime

    return (now_utc() - datetime.fromisoformat(stamp)).total_seconds()


def flatten_all(client, audit) -> int:
    """Cancel every open order, then close every position. Blunt on purpose."""
    canceled = client.cancel_orders()
    closed = client.close_all_positions(cancel_orders=True)
    n = len(closed or [])
    audit.append("flatten", {"orders_canceled": len(canceled or []), "positions_closed": n})
    return n


def tick(store, audit, client, cfg, root: Path, dry_run: bool = False) -> GuardAction:
    account = client.get_account()
    equity = float(account.equity)

    # An assignment is invisible to position reconciliation — the option is gone
    # from both sides, so both agree, while we now hold stock the gate never
    # approved. This is the only place it surfaces.
    try:
        from glassbox import reconcile as _reconcile
        from glassbox.config import require_env
        from glassbox.data.activities import option_events

        events = option_events(
            require_env("ALPACA_API_KEY_ID"), require_env("ALPACA_API_SECRET_KEY")
        )
        assigned = _reconcile.check_assignments(store, audit, events)
        if assigned:
            print(f"ASSIGNMENT detected: {', '.join(assigned)} — halted", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 -- an unavailable activities feed must
        # not stop the equity guards, which are the more important job.
        audit.append("activity_check_error", {"error": f"{type(e).__name__}: {e}"})

    if not store.get_state(SESSION_START_EQUITY_KEY):
        store.set_state(SESSION_START_EQUITY_KEY, str(equity))
    session_start = float(store.get_state(SESSION_START_EQUITY_KEY))
    peak = update_peak_equity(store, equity, broker_peak_equity(client))

    verdict = evaluate_guards(
        equity=equity,
        session_start_equity=session_start,
        peak_equity=peak,
        cfg=cfg,
        kill_switch=kill_switch_engaged(root),
        heartbeat_age_seconds=heartbeat_age(store),
    )

    if verdict.action is GuardAction.CONTINUE:
        already_halted = bool(store.get_state(HALT_KEY))
        if already_halted:
            print(f"[{now_utc():%H:%M:%S}] halted — {store.get_state(HALT_KEY)}")
            return verdict.action
        # A watchdog that prints nothing when healthy is indistinguishable from
        # one that has silently died, so it always reports what it saw.
        print(
            f"[{now_utc():%H:%M:%S}] ok  equity=${equity:,.2f}  "
            f"day={verdict.daily_pnl_pct:+.2f}%  dd={verdict.drawdown_pct:+.2f}%  "
            f"peak=${peak:,.0f}  {verdict.reason}"
        )
        return verdict.action

    audit.append(
        "guard_breach",
        {
            "action": str(verdict.action),
            "reason": verdict.reason,
            "equity": equity,
            "daily_pnl_pct": verdict.daily_pnl_pct,
            "drawdown_pct": verdict.drawdown_pct,
            "dry_run": dry_run,
        },
    )
    store.set_state(HALT_KEY, verdict.reason)
    print(f"GUARD {verdict.action}: {verdict.reason}", file=sys.stderr)
    if not dry_run:
        flatten_all(client, audit)
    return verdict.action


def main() -> int:
    parser = argparse.ArgumentParser(description="GlassBox supervisor")
    parser.add_argument("--interval", type=float, default=15.0, help="poll seconds")
    parser.add_argument("--once", action="store_true", help="single tick then exit")
    parser.add_argument("--dry-run", action="store_true", help="never flatten, just report")
    args = parser.parse_args()

    cfg = load_config()
    root = Path(__file__).resolve().parents[2]
    store = Store(cfg.paths.db)
    audit = AuditLog(cfg.paths.audit_dir, role="supervisor")
    client = supervisor_trading_client()

    audit.append("supervisor_start", {"interval": args.interval, "dry_run": args.dry_run})
    print(f"supervisor watching (interval={args.interval}s, dry_run={args.dry_run})")

    halted = False
    try:
        while True:
            try:
                action = tick(store, audit, client, cfg, root, args.dry_run)
            except Exception as e:  # noqa: BLE001 -- deliberate: a watchdog that
                # dies on a transient broker error leaves the book unguarded, which
                # is strictly worse than logging and retrying on the next tick.
                audit.append("supervisor_error", {"error": f"{type(e).__name__}: {e}"})
                print(f"supervisor error (continuing): {e}", file=sys.stderr)
                action = GuardAction.CONTINUE
            if args.once:
                return 0 if action is GuardAction.CONTINUE else 2
            if action is GuardAction.HALT_HARD and not halted:
                # Stay alive rather than exit. Under a restart policy, exiting
                # produces a crash loop that re-flattens every few seconds and
                # reads as a broken system; and a supervisor that is gone cannot
                # catch a position opened after the halt. It keeps watching,
                # takes no further action, and waits for an operator.
                halted = True
                print(
                    "hard halt — trading stopped, supervisor still watching. "
                    "Clear the kill switch and restart the trader to resume.",
                    file=sys.stderr,
                )
            elif action is not GuardAction.HALT_HARD:
                halted = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        audit.append("supervisor_stop", {})
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
