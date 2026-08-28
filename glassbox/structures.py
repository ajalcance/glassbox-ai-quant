"""Defined-risk option structures — the bandit's action space.

Every structure here has bounded maximum loss by construction. The
`assert_defined_risk` invariant is gate check #4 and must never be bypassed:
within each (right, expiry) group, long contracts must cover short contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Right(StrEnum):
    CALL = "call"
    PUT = "put"


class LegSide(StrEnum):
    LONG = "long"  # BUY_TO_OPEN
    SHORT = "short"  # SELL_TO_OPEN


class StructureKind(StrEnum):
    BULL_PUT_SPREAD = "bull_put_spread"
    BEAR_CALL_SPREAD = "bear_call_spread"
    IRON_CONDOR = "iron_condor"
    CALL_DEBIT_SPREAD = "call_debit_spread"
    PUT_DEBIT_SPREAD = "put_debit_spread"
    LONG_STRANGLE = "long_strangle"


CREDIT_STRUCTURES = frozenset(
    {
        StructureKind.BULL_PUT_SPREAD,
        StructureKind.BEAR_CALL_SPREAD,
        StructureKind.IRON_CONDOR,
    }
)


class UndefinedRiskError(Exception):
    """Raised when a structure contains an uncovered short leg. Never suppress."""


@dataclass(frozen=True, slots=True)
class Leg:
    symbol: str  # OCC option symbol
    right: Right
    strike: float
    expiry: date
    side: LegSide
    ratio_qty: int = 1


@dataclass(frozen=True, slots=True)
class Structure:
    kind: StructureKind
    underlying: str
    legs: tuple[Leg, ...]

    @property
    def is_credit(self) -> bool:
        return self.kind in CREDIT_STRUCTURES

    @property
    def expiry(self) -> date:
        return min(leg.expiry for leg in self.legs)


def assert_defined_risk(structure: Structure) -> None:
    """Structural guarantee of bounded loss. Raises UndefinedRiskError otherwise.

    Rules:
      1. At least two legs (a naked single leg is never allowed).
      2. All legs share one expiry (no calendars — Alpaca rejects uncovered
         short legs in multi-leg calendars anyway).
      3. Within each right (call/put), long contracts >= short contracts.
         Given equal expiry, any long call bounds the loss of a short call at
         any strike; likewise for puts.
    """
    legs = structure.legs
    if len(legs) < 2:
        raise UndefinedRiskError(
            f"{structure.kind}: {len(legs)} leg(s) — a single leg is never defined-risk"
        )

    expiries = {leg.expiry for leg in legs}
    if len(expiries) != 1:
        raise UndefinedRiskError(f"{structure.kind}: mixed expiries {sorted(expiries)}")

    for right in (Right.CALL, Right.PUT):
        long_qty = sum(l.ratio_qty for l in legs if l.right is right and l.side is LegSide.LONG)
        short_qty = sum(l.ratio_qty for l in legs if l.right is right and l.side is LegSide.SHORT)
        if short_qty > long_qty:
            raise UndefinedRiskError(
                f"{structure.kind}: {short_qty} short {right} vs {long_qty} long "
                f"— uncovered short leg"
            )


def max_loss_per_spread(structure: Structure, net_price: float) -> float:
    """Worst-case loss for one spread, in dollars (100x multiplier applied).

    `net_price` is per-share net: positive = debit paid, negative = credit received.
    """
    assert_defined_risk(structure)

    if not structure.is_credit:
        # Debit structures: the most you can lose is what you paid.
        return abs(net_price) * 100

    credit = abs(net_price)
    widths = []
    for right in (Right.CALL, Right.PUT):
        strikes = [l.strike for l in structure.legs if l.right is right]
        if len(strikes) >= 2:
            widths.append(max(strikes) - min(strikes))
    if not widths:
        raise UndefinedRiskError(f"{structure.kind}: credit structure with no spread width")
    # An iron condor can only be breached on one side, so the widest wing governs.
    return (max(widths) - credit) * 100


def structure_key(structure: Structure) -> str:
    """Stable identity for a structure — used in idempotency keys."""
    legs = ":".join(
        f"{l.side}{l.right[0]}{l.strike:g}x{l.ratio_qty}"
        for l in sorted(structure.legs, key=lambda x: (x.right, x.strike, x.side))
    )
    return f"{structure.underlying}|{structure.kind}|{structure.expiry:%Y%m%d}|{legs}"
