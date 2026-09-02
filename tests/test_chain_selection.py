"""Strike selection must choose among strikes the gate could accept.

On 2 Sep, 17 of 20 gate arrivals died on liquidity — AVGO at OI 4, MU at
OI 37 — inside the most liquid underlyings in the market, because nearest()
chose purely by distance while open interest and spread sat unused on the
very quotes it chose from. Selection now prefers liquid strikes and falls
back to the whole chain only when no liquid strike can express the view.
"""

from datetime import date

import pytest

from glassbox.chain import ContractQuote, build_structure, structure_liquidity
from glassbox.config import load_config
from glassbox.structures import Right, StructureKind

CFG = load_config()
EXPIRY = date(2026, 9, 18)
SPOT = 100.0


def quote(strike, right, oi=5000, spread=0.02):
    intrinsic = max(0.0, (SPOT - strike) if right is Right.CALL else (strike - SPOT))
    mid = intrinsic + max(0.05, 3.0 - 0.08 * abs(strike - SPOT))
    return ContractQuote(
        symbol=f"XYZ{EXPIRY:%y%m%d}{right[0].upper()}{int(strike * 1000):08d}",
        right=right,
        strike=float(strike),
        expiry=EXPIRY,
        bid=mid - spread,
        ask=mid + spread,
        open_interest=oi,
    )


def chain(illiquid_strikes=(), wide_strikes=()):
    out = []
    for strike in range(60, 145, 5):
        for right in (Right.CALL, Right.PUT):
            oi = 4 if strike in illiquid_strikes else 5000
            spread = 2.0 if strike in wide_strikes else 0.02
            out.append(quote(strike, right, oi=oi, spread=spread))
    return out


def test_selection_steps_past_an_illiquid_strike():
    """A 4% move targets the 96 put; 95 is the nearest strike. With 95 at
    OI 4 the selector must take a liquid neighbour rather than hand the gate
    a contract it will refuse."""
    liquid = build_structure(StructureKind.BULL_PUT_SPREAD, chain(), SPOT, 4.0, "XYZ", CFG)[0]
    assert liquid.legs[0].strike == 95.0

    s = build_structure(
        StructureKind.BULL_PUT_SPREAD, chain(illiquid_strikes=(95,)), SPOT, 4.0, "XYZ", CFG
    )[0]
    assert s.legs[0].strike != 95.0
    _, oi = structure_liquidity(s, chain(illiquid_strikes=(95,)))
    assert oi >= CFG.gate.min_open_interest


def test_selection_steps_past_a_wide_quote():
    s = build_structure(
        StructureKind.BEAR_CALL_SPREAD, chain(wide_strikes=(105,)), SPOT, 4.0, "XYZ", CFG
    )[0]
    spread_pct, _ = structure_liquidity(s, chain(wide_strikes=(105,)))
    assert spread_pct <= CFG.gate.max_spread_pct_of_mid


def test_selection_falls_back_to_the_whole_chain_when_nothing_is_liquid():
    """The gate keeps the final word: with no liquid strike at all, the old
    behaviour is preserved exactly and the gate refuses as it always did."""
    every = tuple(range(60, 145, 5))
    s = build_structure(
        StructureKind.BULL_PUT_SPREAD, chain(illiquid_strikes=every), SPOT, 4.0, "XYZ", CFG
    )[0]
    assert s.legs[0].strike == 95.0  # nearest, as before
    _, oi = structure_liquidity(s, chain(illiquid_strikes=every))
    assert oi < CFG.gate.min_open_interest  # and the gate will say so


def test_liquidity_may_move_a_leg_but_never_redesign_the_structure():
    """Live check, 2 Sep: AVGO's liquid puts had nothing near the intended
    wing and a pure liquid pick stretched a 2.5-wide spread to 45 points.
    Beyond one intended width the geometrically right strike wins and the
    gate judges its liquidity — the shape of the trade is not negotiable."""
    # everything below 95 is illiquid except a lone liquid strike far away
    illiquid = tuple(k for k in range(60, 95, 5) if k != 60)
    reference = build_structure(StructureKind.BULL_PUT_SPREAD, chain(), SPOT, 4.0, "XYZ", CFG)[0]
    intended_width = reference.legs[0].strike - reference.legs[1].strike
    s = build_structure(
        StructureKind.BULL_PUT_SPREAD, chain(illiquid_strikes=illiquid), SPOT, 4.0, "XYZ", CFG
    )[0]
    assert s.legs[0].strike == pytest.approx(95.0)
    width = s.legs[0].strike - s.legs[1].strike
    assert width <= 2 * intended_width, f"wing stretched to {width} (intended {intended_width})"
    assert s.legs[1].strike != 60.0, "the far liquid strike must not be chosen as the wing"
