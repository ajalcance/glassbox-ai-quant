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
    all_calls, all_puts = _side(chain, Right.CALL), _side(chain, Right.PUT)
    # Choose among strikes the gate could actually accept. nearest() picked
    # purely by distance while open_interest and spread sat unused on the
    # very quotes it chose from — so on 2 Sep it landed on AVGO at OI 4 and
    # MU at OI 37 inside the most liquid names in the market, and 17 of 20
    # gate arrivals died on liquidity. The gate keeps the final word: if no
    # liquid strike exists the full side is used and the gate refuses as
    # before. Selection merely stops handing it candidates that cannot pass.
    calls, puts = _liquid(all_calls, cfg), _liquid(all_puts, cfg)
    return _build_from(
        kind, calls, puts, all_calls, all_puts, spot, expected_move_pct, underlying, cfg, wing_pct
    )


def _liquid(side: list[ContractQuote], cfg) -> list[ContractQuote]:
    """Strikes that clear the gate's own liquidity floor, or the whole side
    if none do."""
    liquid = [
        c
        for c in side
        if c.open_interest >= cfg.gate.min_open_interest
        and c.spread_pct_of_mid <= cfg.gate.max_spread_pct_of_mid
    ]
    return liquid if liquid else side


def _pick(liquid: list[ContractQuote], full: list[ContractQuote], target: float, tolerance: float):
    """The strike nearest `target`, preferring liquid ones — but never so far
    from target that the structure's shape changes.

    Live check on 2 Sep: AVGO's liquid puts had nothing near the intended
    wing, and a pure liquid pick stretched a 2.5-wide spread to 45 points.
    Liquidity may move a leg one notch; it may not redesign the structure.
    Beyond `tolerance` the geometrically right strike wins and the gate
    judges its liquidity as before.
    """
    geometric = nearest(full, target)
    if liquid:
        best = nearest(liquid, target)
        # measured from the strike geometry would have chosen — "N notches
        # from where it would have been", not from a target that may fall
        # between strikes
        if abs(best.strike - geometric.strike) <= tolerance + 1e-9:
            return best
    return geometric


def _build_from(
    kind: StructureKind,
    calls: list[ContractQuote],
    puts: list[ContractQuote],
    all_calls: list[ContractQuote],
    all_puts: list[ContractQuote],
    spot: float,
    expected_move_pct: float,
    underlying: str,
    cfg,
    wing_pct: float,
) -> tuple[Structure, float]:
    # Floor the move so a near-zero forecast cannot pick the same strike twice,
    # which is not a spread.
    move = spot * max(expected_move_pct, cfg.signal.min_move_pct_for_strikes) / 100

    def build(legs: tuple[Leg, ...], net: float) -> tuple[Structure, float]:
        structure = Structure(kind=kind, underlying=underlying, legs=legs)
        assert_defined_risk(structure)
        return structure, round(net, 2)

    def below(side, strike):
        return [c for c in side if c.strike < strike]

    def above(side, strike):
        return [c for c in side if c.strike > strike]

    def at_or_below(side, strike):
        return [c for c in side if c.strike <= strike + 1e-9]

    def at_or_above(side, strike):
        return [c for c in side if c.strike >= strike - 1e-9]

    # The primary leg (the short of a credit, the long of a debit) may move
    # further for liquidity than a wing may: a short strike two notches
    # further out-of-the-money is a more conservative statement of the same
    # view, while a wing two notches wider is a different trade. AAPL, 2 Sep
    # live check: the liquid short sat two notches from target; one width of
    # tolerance fell back to the illiquid strike and the gate vetoed anyway.
    reach = cfg.signal.strike_liquidity_tolerance_widths

    if kind is StructureKind.BULL_PUT_SPREAD:
        width = _wing_width(all_puts, spot, wing_pct)
        # a credit's short sits at or beyond the expected move, liquid or not —
    # "sell beyond where we expect price to reach" applies to the fallback too
        short = _pick(at_or_below(puts, spot - move), at_or_below(all_puts, spot - move),
                      spot - move, width * reach)
        long_ = _pick(below(puts, short.strike), below(all_puts, short.strike),
                      short.strike - width, width)
        if long_.strike >= short.strike:
            raise NoSuitableStrikesError("no put strike below the short leg")
        return build(
            (_leg(short, LegSide.SHORT), _leg(long_, LegSide.LONG)),
            -(short.mid - long_.mid),
        )

    if kind is StructureKind.BEAR_CALL_SPREAD:
        width = _wing_width(all_calls, spot, wing_pct)
        short = _pick(at_or_above(calls, spot + move), at_or_above(all_calls, spot + move),
                      spot + move, width * reach)
        long_ = _pick(above(calls, short.strike), above(all_calls, short.strike),
                      short.strike + width, width)
        if long_.strike <= short.strike:
            raise NoSuitableStrikesError("no call strike above the short leg")
        return build(
            (_leg(short, LegSide.SHORT), _leg(long_, LegSide.LONG)),
            -(short.mid - long_.mid),
        )

    if kind is StructureKind.IRON_CONDOR:
        pw, cw = _wing_width(all_puts, spot, wing_pct), _wing_width(all_calls, spot, wing_pct)
        put_s = _pick(at_or_below(puts, spot - move), at_or_below(all_puts, spot - move),
                      spot - move, pw * reach)
        call_s = _pick(at_or_above(calls, spot + move), at_or_above(all_calls, spot + move),
                      spot + move, cw * reach)
        put_l = _pick(below(puts, put_s.strike), below(all_puts, put_s.strike),
                      put_s.strike - pw, pw)
        call_l = _pick(above(calls, call_s.strike), above(all_calls, call_s.strike),
                       call_s.strike + cw, cw)
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
        width = _wing_width(all_calls, spot, wing_pct)
        long_ = _pick(calls, all_calls, spot, width * reach)
        short = _pick(above(calls, long_.strike), above(all_calls, long_.strike),
                      spot + move, width)
        if short.strike <= long_.strike:
            raise NoSuitableStrikesError("no call strike above the long leg")
        return build(
            (_leg(long_, LegSide.LONG), _leg(short, LegSide.SHORT)),
            long_.mid - short.mid,
        )

    if kind is StructureKind.PUT_DEBIT_SPREAD:
        width = _wing_width(all_puts, spot, wing_pct)
        long_ = _pick(puts, all_puts, spot, width * reach)
        short = _pick(below(puts, long_.strike), below(all_puts, long_.strike),
                      spot - move, width)
        if short.strike >= long_.strike:
            raise NoSuitableStrikesError("no put strike below the long leg")
        return build(
            (_leg(long_, LegSide.LONG), _leg(short, LegSide.SHORT)),
            long_.mid - short.mid,
        )

    if kind is StructureKind.LONG_STRANGLE:
        width = _wing_width(all_calls, spot, wing_pct)
        call = _pick(above(calls, spot), above(all_calls, spot), spot + move, width)
        put = _pick(below(puts, spot), below(all_puts, spot), spot - move, width)
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
