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
SEEN_ASSIGNMENTS_KEY = "seen_assignments"


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


def _leg_quantities(rows) -> dict[str, int]:
    """Signed contract count per option symbol implied by position rows."""
    totals: dict[str, int] = {}
    for row in rows:
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
    """Pure comparison — no side effects. Caller decides whether to halt.

    A position whose entry order is still working is *expected* to be absent
    at the broker — the whole point of an 'opening' status is that the fill
    has not happened yet. Its legs are therefore compared as a band (nothing
    filled … fully filled) rather than demanded outright. Found live on
    1 Sep: the first resting entry order of the contest halted the system 56s
    after submission, because these legs were counted as already held. The
    moment the entry order resolves without a fill, the 'opening' row stops
    being excused and any leftover divergence halts as before.
    """
    in_flight_rows = store.orders_in_flight()
    pending_open_pids = {r["position_id"] for r in in_flight_rows if r["intent"] == "open"}

    settled_rows, pending_rows = [], []
    for row in store.open_positions():
        if row["status"] == "opening" and row["position_id"] in pending_open_pids:
            pending_rows.append(row)
        else:
            settled_rows.append(row)

    settled = _leg_quantities(settled_rows)
    pending = _leg_quantities(pending_rows)
    broker = _broker_leg_quantities(broker_positions)

    local_only = tuple(sorted(set(settled) - set(broker)))
    broker_only = tuple(sorted(set(broker) - set(settled) - set(pending)))
    mismatch = []
    for s in set(broker) & (set(settled) | set(pending)):
        lo, hi = sorted((settled.get(s, 0), settled.get(s, 0) + pending.get(s, 0)))
        if not lo <= broker[s] <= hi:
            mismatch.append(s)
    mismatch = tuple(sorted(mismatch))
    in_flight = tuple(sorted(r["client_order_id"] for r in in_flight_rows))

    ok = not (local_only or broker_only or mismatch)
    return ReconcileResult(
        ok=ok,
        local_only=local_only,
        broker_only=broker_only,
        qty_mismatch=mismatch,
        in_flight=in_flight,
        detail={"local": settled, "pending": pending, "broker": broker},
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


def check_assignments(store, audit, activities) -> tuple[str, ...]:
    """Halt on any newly seen option assignment.

    An assignment is invisible to position reconciliation: the option is gone
    from both our records and the broker's, so both agree — while we now hold
    stock we never chose, without any of the defined-risk properties the gate
    approved. There is no automated response that is obviously right here, so
    the system stops and asks for a human.

    Expirations are recorded but do not halt; a position expiring worthless is
    the normal end of a trade, not a surprise.
    """
    assignments = [a for a in activities if getattr(a, "is_assignment", False)]
    if not assignments:
        return ()

    seen = set(json.loads(store.get_state(SEEN_ASSIGNMENTS_KEY) or "[]"))
    fresh = [a for a in assignments if f"{a.date}:{a.symbol}:{a.qty}" not in seen]
    if not fresh:
        return ()

    seen.update(f"{a.date}:{a.symbol}:{a.qty}" for a in fresh)
    store.set_state(SEEN_ASSIGNMENTS_KEY, json.dumps(sorted(seen)))

    reason = "option assignment: " + ", ".join(str(a) for a in fresh)
    store.set_state(HALT_KEY, reason)
    audit.append(
        "halt",
        {
            "source": "assignment",
            "reason": reason,
            "assignments": [{"symbol": a.symbol, "qty": a.qty, "date": a.date} for a in fresh],
        },
    )
    return tuple(f"{a.symbol}" for a in fresh)
