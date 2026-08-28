"""Three-way position reconciliation.

Compares what we think we hold, what the broker says we hold, and what our
orders imply. Any unexplained divergence HALTS trading: acting on top of a
mismatch compounds an error we do not yet understand.

The broker is truth. Local state is a cache that must be provably in sync.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

HALT_KEY = "halt_reason"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    ok: bool
    local_only: tuple[str, ...] = ()  # we think we hold it; broker disagrees
    broker_only: tuple[str, ...] = ()  # broker holds it; we have no record
    qty_mismatch: tuple[str, ...] = ()  # both know it, quantities differ
    in_flight: tuple[str, ...] = ()  # orders we submitted but never resolved
    detail: dict = field(default_factory=dict)

    @property
    def reason(self) -> str:
        parts = []
        if self.local_only:
            parts.append(f"local-only: {', '.join(self.local_only)}")
        if self.broker_only:
            parts.append(f"broker-only: {', '.join(self.broker_only)}")
        if self.qty_mismatch:
            parts.append(f"qty mismatch: {', '.join(self.qty_mismatch)}")
        if self.in_flight:
            parts.append(f"unresolved orders: {', '.join(self.in_flight)}")
        return "; ".join(parts) or "in sync"


def _local_leg_quantities(store) -> dict[str, int]:
    """Signed contract count per option symbol implied by our open positions."""
    totals: dict[str, int] = {}
    for row in store.open_positions():
        qty = int(row["qty"])
        for leg in json.loads(row["legs_json"]):
            signed = leg["ratio_qty"] * qty * (1 if leg["side"] == "long" else -1)
            totals[leg["symbol"]] = totals.get(leg["symbol"], 0) + signed
    return {k: v for k, v in totals.items() if v != 0}


def _broker_leg_quantities(broker_positions) -> dict[str, int]:
    """Signed contract count per option symbol as reported by Alpaca."""
    totals: dict[str, int] = {}
    for pos in broker_positions:
        symbol = getattr(pos, "symbol", None) or pos["symbol"]
        qty = int(float(getattr(pos, "qty", None) or pos["qty"]))
        if qty:
            totals[symbol] = totals.get(symbol, 0) + qty
    return totals


def reconcile(store, broker_positions) -> ReconcileResult:
    """Pure comparison — no side effects. Caller decides whether to halt."""
    local = _local_leg_quantities(store)
    broker = _broker_leg_quantities(broker_positions)

    local_only = tuple(sorted(set(local) - set(broker)))
    broker_only = tuple(sorted(set(broker) - set(local)))
    mismatch = tuple(sorted(s for s in set(local) & set(broker) if local[s] != broker[s]))
    in_flight = tuple(sorted(r["client_order_id"] for r in store.orders_in_flight()))

    ok = not (local_only or broker_only or mismatch)
    return ReconcileResult(
        ok=ok,
        local_only=local_only,
        broker_only=broker_only,
        qty_mismatch=mismatch,
        in_flight=in_flight,
        detail={"local": local, "broker": broker},
    )


def enforce(store, audit, broker_positions) -> ReconcileResult:
    """Reconcile and persist a HALT if divergent. Returns the result.

    In-flight orders alone do not halt: an order can legitimately be resting.
    Only an actual position divergence does.
    """
    result = reconcile(store, broker_positions)
    if result.ok:
        if store.get_state(HALT_KEY):
            store.set_state(HALT_KEY, "")
            audit.append("resume", {"source": "reconcile", "note": "divergence cleared"})
    else:
        store.set_state(HALT_KEY, result.reason)
        audit.append(
            "halt",
            {"source": "reconcile", "reason": result.reason, "detail": result.detail},
        )
    return result


def is_halted(store) -> bool:
    return bool(store.get_state(HALT_KEY))
