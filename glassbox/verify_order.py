"""Saturday verification: place and cancel one real multi-leg paper order.

    uv run python -m glassbox.verify_order

Builds a far-out-of-the-money SPY bull put spread, submits it as an MLEG limit
order priced so it will NOT fill, confirms the broker accepted it, reconciles,
then cancels. Proves the whole execution path end to end without taking risk.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from alpaca.trading.requests import GetOptionContractsRequest

from glassbox.audit import AuditLog
from glassbox.clock import market_date, parse_expiry
from glassbox.config import load_config
from glassbox.data.alpaca_client import trading_client
from glassbox.execution.ids import client_order_id
from glassbox.execution.router import OrderRouter
from glassbox.reconcile import enforce
from glassbox.store import Store
from glassbox.structures import (
    Leg,
    LegSide,
    Right,
    Structure,
    StructureKind,
    max_loss_per_spread,
    structure_key,
)


def build_far_otm_put_spread(client) -> Structure:
    """Two puts far below spot, same expiry, 5 points apart."""
    req = GetOptionContractsRequest(
        underlying_symbols=["SPY"],
        expiration_date_gte=(market_date() + timedelta(days=7)).isoformat(),
        expiration_date_lte=(market_date() + timedelta(days=45)).isoformat(),
        type="put",
        limit=500,
    )
    contracts = client.get_option_contracts(req).option_contracts
    if not contracts:
        raise RuntimeError("no SPY put contracts returned")

    # Filter expiries client-side: the server-side bound is not reliable and we
    # must never pick a contract expiring inside our minimum-time-to-expiry.
    floor = market_date() + timedelta(days=7)
    by_expiry: dict[date, list] = {}
    for c in contracts:
        exp = parse_expiry(c.expiration_date)
        if exp >= floor:
            by_expiry.setdefault(exp, []).append(c)
    if not by_expiry:
        raise RuntimeError(f"no SPY put expiries on or after {floor}")
    expiry = min(by_expiry)
    strikes = sorted(by_expiry[expiry], key=lambda c: float(c.strike_price))

    # Deep OTM: take from the low end so the spread is nearly worthless.
    long_c, short_c = strikes[0], None
    for c in strikes[1:]:
        if float(c.strike_price) - float(long_c.strike_price) >= 5:
            short_c = c
            break
    if short_c is None:
        raise RuntimeError("could not find a 5-point wide put spread")

    return Structure(
        kind=StructureKind.BULL_PUT_SPREAD,
        underlying="SPY",
        legs=(
            Leg(short_c.symbol, Right.PUT, float(short_c.strike_price), expiry, LegSide.SHORT),
            Leg(long_c.symbol, Right.PUT, float(long_c.strike_price), expiry, LegSide.LONG),
        ),
    )


def main() -> int:
    cfg = load_config()
    client = trading_client()
    store = Store(cfg.paths.db)
    audit = AuditLog(cfg.paths.audit_dir, role="verify-order")
    router = OrderRouter(client, store, audit)

    acct = client.get_account()
    print(f"account {acct.account_number}  equity=${acct.equity}  status={acct.status}")

    structure = build_far_otm_put_spread(client)
    key = structure_key(structure)
    print(f"structure: {key}")

    # Price it as a credit far above anything achievable so it rests unfilled,
    # but still below the spread width so max-loss stays plausible.
    limit_price = -4.50  # below the 5-wide width, far above any real bid -> rests unfilled
    risk = max_loss_per_spread(structure, limit_price)
    print(f"defined-risk check passed; max loss/spread would be ${risk:.2f}")

    coid = client_order_id("verify-run", key)
    order = router.submit_structure(structure, 1, limit_price, coid, "verify-pos")
    print(f"submitted: alpaca_id={order.id} client_order_id={coid} status={order.status}")

    # Idempotency: a retry with the same id must not create a second order.
    router.submit_structure(structure, 1, limit_price, coid, "verify-pos")
    open_orders = [o for o in client.get_orders() if o.client_order_id == coid]
    print(f"idempotency: {len(open_orders)} order(s) at broker for this id (expect 1)")

    result = enforce(store, audit, client.get_all_positions())
    print(f"reconcile: ok={result.ok} — {result.reason}")

    router.cancel(coid, str(order.id))
    print("canceled")

    ok = len(open_orders) == 1 and result.ok
    print("VERIFY PASSED" if ok else "VERIFY FAILED")
    store.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
