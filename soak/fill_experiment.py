"""Does a mid-priced spread order ever fill, or must the limit be marketable?

The trader prices entries at the mid and fills roughly a third of the time.
The hold probe prices to cross the natural and fills every time, on the same
account, host and API. That is suggestive but not proof: those two differ in
structure, symbol, size and time of day as well as in price.

This controls for everything except price. Same underlying, same expiry, same
structure, submitted seconds apart:

    A  mid          the price the trader actually uses
    B  marketable   crossed toward the natural by --cross (default 35%)

Both rest for the trader's own entry timeout, then both are cancelled. What
fills, and how fast, is the measurement.

Why it matters beyond the fill rate: a paper venue generally fills only when
price reaches the limit, while a live options market has market makers who
routinely meet a spread order INSIDE the quoted spread. If B fills instantly
and A never does regardless of market movement, then paper fill rate is a
FLOOR on live fill rate, not a forecast — and "price entries more
aggressively" would be the wrong lesson to take from paper into real money.

DEV ACCOUNT ONLY. It places real orders; --account must be typed out in full,
exactly as the soak requires, and the run aborts if the credentials resolve to
any other account.

    uv run python soak/fill_experiment.py --account PA3CYQV2PBDK --rounds 3
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glassbox.audit import AuditLog
from glassbox.chain import build_structure
from glassbox.config import load_config
from glassbox.data.alpaca_client import (
    option_data_client,
    stock_data_client,
    trading_client,
)
from glassbox.data.market import MarketData
from glassbox.execution.ids import client_order_id
from glassbox.execution.router import OrderRouter
from glassbox.store import Store
from glassbox.structures import StructureKind, structure_key

SYMBOL = "SPY"  # the most liquid chain there is: if mid never fills HERE, it never fills


def _cross(mid: float, fraction: float) -> float:
    """Move a limit toward the market by a fraction of its own size.

    A debit crosses upward (pay more); a credit crosses toward zero (accept
    less). Same direction of concession in both cases.
    """
    return round(mid * (1 + fraction) if mid > 0 else mid * (1 - fraction), 2)


def _outcome(client, coid: str) -> tuple[str, float | None]:
    from alpaca.trading.requests import GetOrdersRequest

    for o in client.get_orders(GetOrdersRequest(status="all", limit=500)):
        if o.client_order_id == coid:
            fill = o.filled_avg_price
            return str(o.status).split(".")[-1].lower(), (float(fill) if fill else None)
    return "missing", None


def main() -> int:
    ap = argparse.ArgumentParser(description="Mid vs marketable fill experiment (dev account)")
    ap.add_argument("--account", required=True,
                    help="paper account this may touch; aborts on any other")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--cross", type=float, default=0.35,
                    help="how far arm B crosses toward the natural, as a fraction of mid")
    ap.add_argument("--kind", default="call_debit_spread")
    args = ap.parse_args()

    cfg = load_config()
    client = trading_client()
    account = client.get_account()
    if account.account_number != args.account:
        print(f"REFUSING: account {account.account_number} != expected {args.account}")
        return 2
    if not client.get_clock().is_open:
        print("REFUSING: market closed — an experiment about fills needs a live book")
        return 2

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    workdir = ROOT / "soak-results" / f"fills-{run_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    store = Store(workdir / "fills.db")
    audit = AuditLog(workdir / "audit", role="fill-experiment")
    router = OrderRouter(client, store, audit)
    data = MarketData(trading_client=client, stock_client=stock_data_client(),
                      option_client=option_data_client(), store=store, root=ROOT)

    wait = cfg.execution.entry_fill_timeout_minutes * 60
    print(f"FILL EXPERIMENT {run_id} — account {account.account_number}")
    print(f"  {args.rounds} rounds on {SYMBOL} {args.kind}, resting {wait:.0f}s each")
    print(f"  arm A = mid (what the trader uses) · arm B = mid crossed {args.cross:.0%}\n")

    coids: list[str] = []
    results: list[dict] = []
    try:
        for rnd in range(args.rounds):
            spot = data.spot(SYMBOL)
            chain = data.chain(SYMBOL, horizon_hours=48)
            if not chain:
                print("  no tradable chain; stopping")
                break
            structure, mid = build_structure(
                StructureKind(args.kind), chain, spot, 1.5, SYMBOL, cfg
            )
            arms = {"A_mid": round(mid, 2), "B_marketable": _cross(mid, args.cross)}
            print(f"round {rnd}: {structure_key(structure)}")
            print(f"  spot {spot:.2f}  mid {mid:+.2f}  ->  A {arms['A_mid']:+.2f} · "
                  f"B {arms['B_marketable']:+.2f}")

            submitted = {}
            for arm, price in arms.items():
                sid = f"fillx-{run_id}-{rnd}-{arm}"
                coid = client_order_id(sid, structure_key(structure))
                coids.append(coid)
                try:
                    router.submit_structure(structure, 1, price, coid, f"pos-{sid}")
                    submitted[arm] = (coid, price)
                except Exception as e:  # noqa: BLE001 -- a refused arm is a result
                    print(f"    {arm}: submit refused — {type(e).__name__}: {e}")

            # Poll both together so neither gets a timing advantage.
            filled_at: dict[str, float] = {}
            started = time.monotonic()
            while time.monotonic() - started < wait and len(filled_at) < len(submitted):
                time.sleep(5)
                for arm, (coid, _price) in submitted.items():
                    if arm in filled_at:
                        continue
                    status, _fill = _outcome(client, coid)
                    if status == "filled":
                        filled_at[arm] = time.monotonic() - started
                        print(f"    {arm}: FILLED after {filled_at[arm]:.0f}s")

            for arm, (coid, price) in submitted.items():
                status, fill = _outcome(client, coid)
                if status not in ("filled", "canceled", "expired"):
                    with contextlib.suppress(Exception):
                        for o in client.get_orders():
                            if o.client_order_id == coid:
                                client.cancel_order_by_id(o.id)
                    print(f"    {arm}: unfilled after {wait:.0f}s — cancelled")
                results.append({
                    "round": rnd, "arm": arm, "limit": price, "mid": round(mid, 2),
                    "status": status, "fill": fill,
                    "seconds_to_fill": round(filled_at[arm]) if arm in filled_at else None,
                })
            print()

        # ---- the answer ----
        by_arm: dict[str, list[dict]] = {}
        for r in results:
            by_arm.setdefault(r["arm"], []).append(r)
        print("=" * 62)
        for arm, rows in sorted(by_arm.items()):
            fills = [r for r in rows if r["status"] == "filled"]
            times = [r["seconds_to_fill"] for r in fills if r["seconds_to_fill"] is not None]
            avg = f", median {sorted(times)[len(times) // 2]}s" if times else ""
            print(f"  {arm:14s} {len(fills)}/{len(rows)} filled{avg}")
        a = sum(1 for r in by_arm.get("A_mid", []) if r["status"] == "filled")
        b = sum(1 for r in by_arm.get("B_marketable", []) if r["status"] == "filled")
        print("=" * 62)
        if b > a and a == 0:
            print("  Mid NEVER filled while marketable always did: this venue fills only on")
            print("  a marketable limit. Paper fill rate is a FLOOR on live, not a forecast —")
            print("  do not carry 'price more aggressively' from paper into real money.")
        elif a > 0 and b > 0:
            print("  Both arms fill: price is not the whole story, and the trader's mid")
            print("  pricing is not the reason entries expire. Look elsewhere.")
        elif a == 0 and b == 0:
            print("  Neither filled: the experiment is inconclusive — the market did not")
            print("  move onto either limit. Re-run in a more active session.")
        (workdir / "results.json").write_text(json.dumps(results, indent=2))
        print(f"\n  raw: {workdir / 'results.json'}")
    finally:
        # Never leave residue: cancel anything of ours still resting.
        try:
            from alpaca.trading.requests import GetOrdersRequest

            for o in client.get_orders(GetOrdersRequest(status="open", limit=500)):
                if o.client_order_id in coids:
                    with contextlib.suppress(Exception):
                        client.cancel_order_by_id(o.id)
        except Exception as e:  # noqa: BLE001 -- the sweep failing IS the finding
            print(f"  cleanup sweep error (non-fatal): {type(e).__name__}: {e}")
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
