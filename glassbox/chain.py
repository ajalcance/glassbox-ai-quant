"""Option chain selection — turning a view into concrete strikes.

The edge test says *what* to express; this decides *where*. Strike choice is
driven by the expected move rather than a fixed delta, because the expected move
is the thing we actually formed an opinion about:

  * Credit structures sell beyond where we expect price to reach, so the short
    strike sits past the expected move and we are paid for the distance.
  * Debit structures buy at the money and sell at the expected move, so the
    short leg caps the trade exactly where the thesis says it stops working.

Everything returned is defined-risk by construction, and every candidate is
re-checked with assert_defined_risk before it leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from glassbox.structures import (
    Leg,
    LegSide,
    Right,
    Structure,
    StructureKind,
    assert_defined_risk,
)


class NoSuitableStrikesError(Exception):
    """The chain cannot express this view — skip the signal rather than force it."""


@dataclass(frozen=True, slots=True)
class ContractQuote:
    symbol: str
    right: Right
    strike: float
    expiry: date
    bid: float
    ask: float
    open_interest: int = 0
    implied_volatility: float | None = None
    delta: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct_of_mid(self) -> float:
        mid = self.mid
        return 100 * (self.ask - self.bid) / mid if mid > 0 else float("inf")

    @property
    def tradable(self) -> bool:
        return self.bid > 0 and self.ask > self.bid


def _side(chain: list[ContractQuote], right: Right) -> list[ContractQuote]:
    return sorted((c for c in chain if c.right is right and c.tradable), key=lambda c: c.strike)


def nearest(contracts: list[ContractQuote], target_strike: float) -> ContractQuote:
    if not contracts:
        raise NoSuitableStrikesError("no tradable contracts on this side of the chain")
    return min(contracts, key=lambda c: abs(c.strike - target_strike))


def atm_straddle_mid(chain: list[ContractQuote], spot: float) -> float:
    """Price of the at-the-money straddle — the market's expected move."""
    calls, puts = _side(chain, Right.CALL), _side(chain, Right.PUT)
    if not calls or not puts:
        raise NoSuitableStrikesError("chain missing a tradable side")
    return nearest(calls, spot).mid + nearest(puts, spot).mid


def _wing_width(contracts: list[ContractQuote], spot: float, target_pct: float) -> float:
    """Spread width in points, from the chain's own strike spacing.

    Using the real increment avoids requesting strikes that do not exist.
    """
    strikes = sorted({c.strike for c in contracts})
    if len(strikes) < 2:
        raise NoSuitableStrikesError("need at least two strikes to build a spread")
    increment = min(b - a for a, b in pairwise(strikes) if b > a)
    target = spot * target_pct / 100
    steps = max(1, round(target / increment))
    return increment * steps


def _leg(c: ContractQuote, side: LegSide) -> Leg:
    return Leg(c.symbol, c.right, c.strike, c.expiry, side)


def build_structure(
    kind: StructureKind,
    chain: list[ContractQuote],
    spot: float,
    expected_move_pct: float,
    underlying: str,
    cfg=None,
) -> tuple[Structure, float]:
    """Return the structure and its net price (+ debit paid, - credit received).

    Raises NoSuitableStrikesError when the chain cannot express the view. A
    signal we cannot express cleanly is skipped, never approximated.

    `cfg` supplies the geometry (wing width, minimum move); it defaults to the
    loaded config so drills and tests can call this without threading it.
    """
    if spot <= 0:
        raise NoSuitableStrikesError(f"invalid spot {spot}")
    if cfg is None:
        from glassbox.config import load_config

        cfg = load_config()
    wing_pct = cfg.signal.wing_width_pct
    calls, puts = _side(chain, Right.CALL), _side(chain, Right.PUT)
    # Floor the move so a near-zero forecast cannot pick the same strike twice,
    # which is not a spread.
    move = spot * max(expected_move_pct, cfg.signal.min_move_pct_for_strikes) / 100

    def build(legs: tuple[Leg, ...], net: float) -> tuple[Structure, float]:
        structure = Structure(kind=kind, underlying=underlying, legs=legs)
        assert_defined_risk(structure)
        return structure, round(net, 2)

    if kind is StructureKind.BULL_PUT_SPREAD:
        short = nearest(puts, spot - move)
        width = _wing_width(puts, spot, wing_pct)
        long_ = nearest([p for p in puts if p.strike < short.strike], short.strike - width)
        if long_.strike >= short.strike:
            raise NoSuitableStrikesError("no put strike below the short leg")
        return build(
            (_leg(short, LegSide.SHORT), _leg(long_, LegSide.LONG)),
            -(short.mid - long_.mid),
        )

    if kind is StructureKind.BEAR_CALL_SPREAD:
        short = nearest(calls, spot + move)
        width = _wing_width(calls, spot, wing_pct)
        long_ = nearest([c for c in calls if c.strike > short.strike], short.strike + width)
        if long_.strike <= short.strike:
            raise NoSuitableStrikesError("no call strike above the short leg")
        return build(
            (_leg(short, LegSide.SHORT), _leg(long_, LegSide.LONG)),
            -(short.mid - long_.mid),
        )

    if kind is StructureKind.IRON_CONDOR:
        put_s, call_s = nearest(puts, spot - move), nearest(calls, spot + move)
        pw, cw = _wing_width(puts, spot, wing_pct), _wing_width(calls, spot, wing_pct)
        put_l = nearest([p for p in puts if p.strike < put_s.strike], put_s.strike - pw)
        call_l = nearest([c for c in calls if c.strike > call_s.strike], call_s.strike + cw)
        if put_l.strike >= put_s.strike or call_l.strike <= call_s.strike:
            raise NoSuitableStrikesError("chain too narrow for a condor")
        credit = (put_s.mid - put_l.mid) + (call_s.mid - call_l.mid)
        return build(
            (
                _leg(put_s, LegSide.SHORT),
                _leg(put_l, LegSide.LONG),
                _leg(call_s, LegSide.SHORT),
                _leg(call_l, LegSide.LONG),
            ),
            -credit,
        )

    if kind is StructureKind.CALL_DEBIT_SPREAD:
        long_ = nearest(calls, spot)
        short = nearest([c for c in calls if c.strike > long_.strike], spot + move)
        if short.strike <= long_.strike:
            raise NoSuitableStrikesError("no call strike above the long leg")
        return build(
            (_leg(long_, LegSide.LONG), _leg(short, LegSide.SHORT)),
            long_.mid - short.mid,
        )

    if kind is StructureKind.PUT_DEBIT_SPREAD:
        long_ = nearest(puts, spot)
        short = nearest([p for p in puts if p.strike < long_.strike], spot - move)
        if short.strike >= long_.strike:
            raise NoSuitableStrikesError("no put strike below the long leg")
        return build(
            (_leg(long_, LegSide.LONG), _leg(short, LegSide.SHORT)),
            long_.mid - short.mid,
        )

    if kind is StructureKind.LONG_STRANGLE:
        call = nearest([c for c in calls if c.strike > spot], spot + move)
        put = nearest([p for p in puts if p.strike < spot], spot - move)
        # Long both sides: defined-risk because the debit is the whole exposure,
        # but assert_defined_risk needs a covering leg per right, so a strangle
        # is only permitted as two longs.
        return build((_leg(call, LegSide.LONG), _leg(put, LegSide.LONG)), call.mid + put.mid)

    raise NoSuitableStrikesError(f"unsupported structure {kind}")


def structure_liquidity(structure: Structure, chain: list[ContractQuote]) -> tuple[float, int]:
    """Worst spread and lowest open interest across the legs — a spread is only
    as liquid as its least liquid leg."""
    by_symbol = {c.symbol: c for c in chain}
    quotes = [by_symbol[leg.symbol] for leg in structure.legs if leg.symbol in by_symbol]
    if not quotes:
        return float("inf"), 0
    return max(q.spread_pct_of_mid for q in quotes), min(q.open_interest for q in quotes)
