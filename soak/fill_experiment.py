"""Why do the trader's entries expire? Vary one thing at a time and find out.

The trader fills roughly a third of its entries. This isolates the cause by
holding everything constant except a single variable, starting from a
baseline that fills reliably.

    --vary price     mid vs a limit crossed toward the natural
    --vary qty       the same structure at 1, 5, 10 contracts
    --vary symbol    the same structure across underlyings

RESULT SO FAR (9 Sep, price): REFUTED. Mid and marketable both filled 3/3 in
about five seconds. The venue does not require a crossing limit, and the
trader's mid pricing is not why entries expire. That also corrected a wrong
inference about the hold probe: it fills because it is SPY, quantity 1, near
the money — not because it crosses.

That experiment earned its keep by PREVENTING a change. "Price entries more
aggressively" would have paid real spread on every trade forever to fix a
problem that does not exist.

Remaining candidates, none of which the raw fill data separates on its own
(TLT filled at quantity 17 while AVGO expired at quantity 1, so it is likely
an interaction): order size, underlying liquidity, strike distance and leg
count. Hence one variable at a time rather than another guess.

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
    ap.add_argument("--vary", default="price", choices=("price", "qty", "symbol"),
                    help="the single variable under test; everything else is held fixed")
    ap.add_argument("--cross", type=float, default=0.35,
                    help="--vary price: how far the marketable arm crosses, as a fraction of mid")
    ap.add_argument("--quantities", type=int, nargs="+", default=[1, 5, 10],
                    help="--vary qty: contract counts to compare")
    ap.add_argument("--symbols", nargs="+", default=["SPY", "AAPL", "ADBE"],
                    help="--vary symbol: underlyings to compare, most liquid first")
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
    print(f"  varying {args.vary.upper()} · {args.rounds} rounds · {args.kind} · "
          f"resting {wait:.0f}s each")
    print("  everything else held at the baseline that already fills\n")

    coids: list[str] = []
    results: list[dict] = []
    arm_meta: dict[str, dict] = {}
    try:
        for rnd in range(args.rounds):
            # Each arm: (label, symbol, qty, price-adjust). Only the variable
            # under test differs; everything else is held at the baseline that
            # already fills.
            if args.vary == "price":
                specs = [("A_mid", SYMBOL, 1, 0.0), ("B_marketable", SYMBOL, 1, args.cross)]
            elif args.vary == "qty":
                specs = [(f"qty_{q:02d}", SYMBOL, q, 0.0) for q in args.quantities]
            else:
                specs = [(sym, sym, 1, 0.0) for sym in args.symbols]

            print(f"round {rnd}:")
            submitted = {}
            for arm, sym, qty, adjust in specs:
                try:
                    spot = data.spot(sym)
                    chain = data.chain(sym, horizon_hours=48)
                    if not chain:
                        print(f"    {arm}: no tradable chain")
                        continue
                    structure, mid = build_structure(
                        StructureKind(args.kind), chain, spot, 1.5, sym, cfg
                    )
                except Exception as e:  # noqa: BLE001 -- an unbuildable arm is a result
                    print(f"    {arm}: no structure — {type(e).__name__}: {e}")
                    continue
                price = round(mid, 2) if adjust == 0.0 else _cross(mid, adjust)
                sid = f"fillx-{run_id}-{rnd}-{arm}"
                coid = client_order_id(sid, structure_key(structure))
                coids.append(coid)
                print(f"    {arm:14s} {structure_key(structure)[:46]:46s} "
                      f"x{qty} @ {price:+.2f}")
                try:
                    router.submit_structure(structure, qty, price, coid, f"pos-{sid}")
                    submitted[arm] = (coid, price)
                    arm_meta[arm] = {"symbol": sym, "qty": qty, "mid": round(mid, 2)}
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
                    "round": rnd, "arm": arm, "limit": price,
                    **arm_meta.get(arm, {}),
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
        print("=" * 62)
        rates = {
            arm: sum(1 for r in rows if r["status"] == "filled") / len(rows)
            for arm, rows in by_arm.items() if rows
        }
        if not rates:
            print("  No arm was submitted — nothing to conclude.")
        elif all(v == 0 for v in rates.values()):
            print(f"  NOTHING filled. Inconclusive rather than informative about {args.vary}:")
            print("  the market moved onto no limit at all. Re-run in an active session.")
        elif all(v == 1 for v in rates.values()):
            print(f"  Every arm filled: {args.vary.upper()} does not explain the trader's")
            print("  expiries either. Vary the next candidate.")
        else:
            best = max(rates, key=lambda k: rates[k])
            worst = min(rates, key=lambda k: rates[k])
            print(f"  {args.vary.upper()} SEPARATES the arms: {best} filled "
                  f"{rates[best]:.0%} while {worst} filled {rates[worst]:.0%}.")
            print("  This is a real difference on identical inputs otherwise — the strongest")
            print("  lead so far. Confirm with more rounds before acting on it.")
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
