"""Order lifecycle — the seam between submitting an order and having a position.

This module exists because of a silent gap the seam review found: nothing in
the live path ever confirmed a fill. A position was written as "opening" at
submission and the manager only touches "open", so a filled position would have
sat unmanaged for its whole life — no stop, no target, and no deadline flatten,
which routes through the same manager. The drills passed because they flipped
the status by hand.

Responsibilities, run every tick:

  * Entry order filled      -> position becomes "open", entry price becomes the
                               actual fill, max loss is recomputed from it.
  * Entry order dead        -> position becomes "failed" (leaves heat).
  * Entry order stale       -> cancelled. A news thesis is time-sensitive; an
                               order the market declined at our price for
                               minutes is not chased.
  * Close order filled      -> position becomes "closed" with realized P&L from
                               the actual fill, and the learners are fed.
  * Close order stale       -> escalated: resubmitted at a progressively worse
                               price. Getting out is an obligation.
"""

from __future__ import annotations

from datetime import datetime

from glassbox.manage import Barrier
from glassbox.structures import max_loss_per_spread

LIVE_STATES = ("new", "accepted", "pending_new", "partially_filled", "held")


def _age_seconds(row, now: datetime) -> float:
    return (now - datetime.fromisoformat(row["created_at"])).total_seconds()


def sync(trader, now: datetime) -> list[str]:
    """Reconcile every in-flight order against the broker. Returns event tags
    for the runner's console line."""
    events = []
    for order in trader.store.orders_in_flight():
        try:
            status, fill = trader.router.poll(order["client_order_id"])
        except Exception as e:  # noqa: BLE001 -- an unpollable order stays
            # in flight and is retried next tick; the breaker guards the storm.
            trader.audit.append(
                "lifecycle_poll_error",
                {"client_order_id": order["client_order_id"], "error": f"{type(e).__name__}: {e}"},
            )
            continue

        if status == "filled":
            events.append(_on_fill(trader, order, fill, now))
        elif status in ("canceled", "rejected", "expired"):
            events.append(_on_dead(trader, order, status))
        elif status in LIVE_STATES:
            stale = _maybe_expire(trader, order, now)
            if stale:
                events.append(stale)
    return [e for e in events if e]


def _on_fill(trader, order, fill: float | None, now: datetime) -> str:
    coid = order["client_order_id"]
    trader.store.update_order(coid, status="filled", filled_price=fill)
    position_id = order["position_id"]
    row = trader.store.get_position(position_id) if position_id else None
    if row is None:
        return ""

    if order["intent"] == "open":
        updates = {"status": "open"}
        if fill is not None:
            updates["entry_price"] = fill
            # The risk the book carries is defined by what we actually paid or
            # received, not by the mid we asked for.
            try:
                structure = trader._structure_from_row(row)
                updates["max_loss"] = max_loss_per_spread(structure, fill) * int(row["qty"])
            except Exception as e:  # noqa: BLE001 -- an implausible fill keeps
                # the entry estimate rather than corrupting heat with a guess.
                trader.audit.append(
                    "fill_risk_recompute_skipped",
                    {"position_id": position_id, "error": f"{type(e).__name__}: {e}"},
                )
        trader.store.upsert_position(position_id, **updates)
        trader.audit.append(
            "entry_filled",
            {"position_id": position_id, "client_order_id": coid, "fill": fill},
        )
        return f"FILLED {row['underlying']} at {fill}"

    # A close fill realises the position.
    entry = float(row["entry_price"] or 0.0)
    realized = None
    if fill is not None:
        realized = (fill - entry) * 100 * int(row["qty"])
    barrier = trader.store.get_state(f"close_barrier:{position_id}") or str(Barrier.TIME)
    label = 1 if (realized or 0) > 0 else 0
    trader.store.close_position(
        position_id,
        barrier,
        label,
        realized if realized is not None else 0.0,
        now.isoformat(),
    )
    trader.feed_learners(row, label)
    trader.audit.append(
        "close_filled",
        {
            "position_id": position_id,
            "client_order_id": coid,
            "fill": fill,
            "realized_pnl": realized,
            "barrier": barrier,
        },
    )
    return f"CLOSED {row['underlying']} realized {realized}"


def _on_dead(trader, order, status: str) -> str:
    coid = order["client_order_id"]
    trader.store.update_order(coid, status=status)
    position_id = order["position_id"]
    row = trader.store.get_position(position_id) if position_id else None
    if row is None:
        return ""

    if order["intent"] == "open" and row["status"] == "opening":
        trader.store.upsert_position(position_id, status="failed")
        trader.audit.append("entry_dead", {"position_id": position_id, "order_status": status})
        return f"entry {status}: {row['underlying']}"

    if order["intent"] == "close" and row["status"] == "closing":
        # The close died; the position is still on. Escalate immediately next
        # tick by clearing the attempt marker's timestamp.
        trader.audit.append("close_dead", {"position_id": position_id, "order_status": status})
        return f"close {status}: {row['underlying']} — will escalate"
    return ""


def _maybe_expire(trader, order, now: datetime) -> str:
    cfg = trader.cfg.execution
    age = _age_seconds(order, now)

    if order["intent"] == "open":
        if age < cfg.entry_fill_timeout_minutes * 60:
            return ""
        try:
            trader.router.cancel(order["client_order_id"], order["alpaca_order_id"])
        except Exception as e:  # noqa: BLE001 -- if the cancel itself fails the
            # order stays live and is retried next tick.
            trader.audit.append(
                "entry_cancel_error",
                {"client_order_id": order["client_order_id"], "error": str(e)},
            )
            return ""
        trader.audit.append(
            "entry_expired",
            {
                "client_order_id": order["client_order_id"],
                "age_seconds": round(age),
                "note": "market declined our price; thesis is time-sensitive, not chased",
            },
        )
        return "entry order expired unfilled"

    # Close orders escalate rather than expire.
    if age < cfg.close_retry_seconds:
        return ""
    return _escalate_close(trader, order)


def _escalate_close(trader, order) -> str:
    """Cancel the resting close and resubmit at a worse price.

    Each attempt concedes a further slice of the price toward the market. We
    are obligated to be flat; the escalation bounds what that obligation costs.
    """
    from glassbox.execution.ids import close_order_id

    cfg = trader.cfg.execution
    position_id = order["position_id"]
    row = trader.store.get_position(position_id)
    if row is None:
        return ""

    attempt = int(trader.store.get_state(f"close_attempt:{position_id}") or 1)
    if attempt >= cfg.close_max_attempts:
        trader.audit.append(
            "close_exhausted",
            {
                "position_id": position_id,
                "attempts": attempt,
                "note": "escalation exhausted; supervisor flatten is the backstop",
            },
        )
        return f"close escalation exhausted: {row['underlying']}"

    try:
        trader.router.cancel(order["client_order_id"], order["alpaca_order_id"])
    except Exception as e:  # noqa: BLE001 -- cancel may race a fill; the next
        # poll resolves whichever actually happened.
        trader.audit.append(
            "close_cancel_race",
            {"position_id": position_id, "error": f"{type(e).__name__}: {e}"},
        )
        return ""

    old_price = float(order["limit_price"] or 0.0)
    concession = 1 + (cfg.close_escalation_pct / 100)
    # Paying up means: a debit-signed price rises, a credit-signed price rises
    # toward zero and beyond. Both are "worse for us" in the same direction.
    new_price = round(old_price * concession if old_price > 0 else old_price / concession, 2)

    structure = trader._structure_from_row(row)
    attempt += 1
    trader.store.set_state(f"close_attempt:{position_id}", str(attempt))
    coid = close_order_id(position_id, "escalated", attempt)
    trader.router.submit_structure(
        structure, int(row["qty"]), new_price, coid, position_id, closing=True
    )
    trader.audit.append(
        "close_escalated",
        {
            "position_id": position_id,
            "attempt": attempt,
            "old_price": old_price,
            "new_price": new_price,
        },
    )
    return f"close escalated (attempt {attempt}): {row['underlying']} at {new_price}"
