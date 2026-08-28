"""Order router — the only path from a decision to the broker.

Guarantees:
  * Idempotent. Deterministic client_order_id; a retry after a timeout cannot
    double-fill. The intent is persisted BEFORE submission so a crash mid-call
    still leaves something to reconcile.
  * Defined-risk. assert_defined_risk runs again here, at the last moment
    before submission — belt and braces with gate check #4.
  * Atomic. Spreads go as MLEG orders, so we are never left holding one leg.
  * Never market orders. Option spreads are wide; we work a limit and walk it.
  * Circuit-broken. Broker failures open the breaker instead of storming it.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from glassbox.execution.breaker import CircuitBreaker
from glassbox.structures import LegSide, Structure, assert_defined_risk, structure_key


class OrderRejected(Exception):
    """Broker refused the order — not retryable without changing something."""


def _leg_requests(structure: Structure, closing: bool = False) -> list[OptionLegRequest]:
    """Map our legs to Alpaca leg requests. Closing inverts every side."""
    legs = []
    for leg in structure.legs:
        long_leg = leg.side is LegSide.LONG
        if closing:
            long_leg = not long_leg
        legs.append(
            OptionLegRequest(
                symbol=leg.symbol,
                ratio_qty=leg.ratio_qty,
                side=OrderSide.BUY if long_leg else OrderSide.SELL,
                position_intent=(
                    PositionIntent.BUY_TO_CLOSE
                    if closing and long_leg
                    else PositionIntent.SELL_TO_CLOSE
                    if closing
                    else PositionIntent.BUY_TO_OPEN
                    if long_leg
                    else PositionIntent.SELL_TO_OPEN
                ),
            )
        )
    return legs


def legs_as_dicts(structure: Structure) -> list[dict]:
    """Serialisable leg form for the store and audit log."""
    return [
        {
            "symbol": l.symbol,
            "right": str(l.right),
            "strike": l.strike,
            "expiry": l.expiry.isoformat(),
            "side": str(l.side),
            "ratio_qty": l.ratio_qty,
        }
        for l in structure.legs
    ]


@dataclass(frozen=True, slots=True)
class PriceLadder:
    """Walk a limit from mid toward marketable, in ticks, and give up.

    We never cross the whole spread: `max_steps` bounds how much edge we pay.
    """

    start: float
    tick: float = 0.01
    max_steps: int = 4
    is_debit: bool = True

    def price_at(self, step: int) -> float:
        step = max(0, min(step, self.max_steps))
        # Debits walk up (pay more); credits walk down (accept less).
        delta = self.tick * step
        price = self.start + delta if self.is_debit else self.start - delta
        return round(price, 2)


class OrderRouter:
    def __init__(self, trading_client, store, audit, breaker: CircuitBreaker | None = None):
        self.client = trading_client
        self.store = store
        self.audit = audit
        self.breaker = breaker or CircuitBreaker()

    def submit_structure(
        self,
        structure: Structure,
        qty: int,
        limit_price: float,
        client_order_id: str,
        position_id: str,
        closing: bool = False,
    ):
        """Submit one multi-leg order. Safe to call again with the same
        client_order_id — a duplicate is detected and the existing order
        returned rather than a second position opened."""
        assert_defined_risk(structure)  # last line of defence before the wire
        if qty < 1:
            raise ValueError(f"qty must be >= 1, got {qty}")

        existing = self.store.get_order(client_order_id)
        if existing and existing["status"] in ("submitted", "filled"):
            self.audit.append(
                "order_duplicate_suppressed",
                {"client_order_id": client_order_id, "status": existing["status"]},
            )
            return existing

        legs = legs_as_dicts(structure)
        self.store.record_order(
            client_order_id=client_order_id,
            intent="close" if closing else "open",
            legs=legs,
            limit_price=limit_price,
            position_id=position_id,
        )

        request = LimitOrderRequest(
            qty=qty,
            limit_price=limit_price,
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            legs=_leg_requests(structure, closing=closing),
        )

        self.audit.append(
            "order_submit",
            {
                "client_order_id": client_order_id,
                "position_id": position_id,
                "structure": structure_key(structure),
                "qty": qty,
                "limit_price": limit_price,
                "closing": closing,
                "legs": legs,
            },
        )

        try:
            order = self.breaker.call(lambda: self.client.submit_order(request))
        except Exception as e:
            self.store.update_order(client_order_id, status="rejected")
            self.audit.append(
                "order_error",
                {"client_order_id": client_order_id, "error": f"{type(e).__name__}: {e}"},
            )
            raise

        self.store.update_order(
            client_order_id,
            status="submitted",
            alpaca_order_id=str(order.id),
        )
        return order

    def cancel(self, client_order_id: str, alpaca_order_id: str) -> None:
        try:
            self.breaker.call(lambda: self.client.cancel_order_by_id(alpaca_order_id))
        except Exception as e:
            self.audit.append(
                "cancel_error",
                {"client_order_id": client_order_id, "error": f"{type(e).__name__}: {e}"},
            )
            raise
        self.store.update_order(client_order_id, status="canceled")
        self.audit.append("order_cancel", {"client_order_id": client_order_id})
