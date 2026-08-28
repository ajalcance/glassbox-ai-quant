"""The edge test — the actual signal.

The tradable question is never "is this good news". It is: **is the move I
expect larger or smaller than the move the options are already pricing?** Good
news already priced in is not an opportunity; dull news priced for chaos is.

Implied move comes from the at-the-money straddle, which is the market's own
estimate of the absolute move to expiry, time-scaled to our horizon by the
square root of time. Expected move comes from the analyst. Their ratio decides
whether we want to be long or short optionality — or, most often, neither.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from glassbox.structures import StructureKind


class EdgeVerdict(StrEnum):
    LONG_CONVEXITY = "long_convexity"  # options look cheap -> pay a debit
    SHORT_PREMIUM = "short_premium"  # options look rich  -> take a credit
    NO_EDGE = "no_edge"


@dataclass(frozen=True, slots=True)
class EdgeResult:
    verdict: EdgeVerdict
    expected_move_pct: float
    implied_move_pct: float
    ratio: float
    eligible_structures: tuple[StructureKind, ...]
    detail: str

    @property
    def tradable(self) -> bool:
        return self.verdict is not EdgeVerdict.NO_EDGE


def implied_move_pct(
    straddle_mid: float,
    spot: float,
    hours_to_expiry: float,
    horizon_hours: float,
    min_horizon_hours: float = 6.5,
) -> float:
    """Market-implied absolute move over `horizon_hours`, as a percentage.

    The straddle prices the move to expiry and volatility scales with the square
    root of time, so a shorter horizon implies a smaller move.

    That scaling assumes *diffusion* — volatility accumulating smoothly — so it
    is only valid for comparing quiet periods of different lengths. It is the
    wrong model for an event, and `evaluate_edge` does not use it: see
    `event_implied_move_pct`.
    """
    if spot <= 0 or straddle_mid <= 0:
        raise ValueError(f"invalid quote: straddle={straddle_mid}, spot={spot}")
    if hours_to_expiry <= 0:
        raise ValueError("hours_to_expiry must be positive")

    effective_horizon = max(horizon_hours, min_horizon_hours)
    move_to_expiry = 100 * straddle_mid / spot
    scale = math.sqrt(min(effective_horizon, hours_to_expiry) / hours_to_expiry)
    return move_to_expiry * scale


def event_implied_move_pct(straddle_mid: float, spot: float) -> float:
    """The market's expected absolute move *through the event*, unscaled.

    For event-driven trades this is the honest comparison and the one options
    traders actually make: the at-the-money straddle on an expiry that spans the
    event already embeds the market's estimate of the jump. Buying that straddle
    buys the event, so "is the straddle too cheap or too rich for the move I
    expect" is answered by comparing directly against it.

    Time-scaling would be double counting. A news jump does not accumulate over
    the horizon — it happens once — and discounting the straddle by sqrt(t) down
    to the analyst's stated horizon makes the market look as though it prices
    almost nothing for an event that is hours away. That inflates every ratio
    and biases the system into paying up for optionality it does not need.
    """
    if spot <= 0 or straddle_mid <= 0:
        raise ValueError(f"invalid quote: straddle={straddle_mid}, spot={spot}")
    return 100 * straddle_mid / spot


def eligible_structures(verdict: EdgeVerdict, direction: str) -> tuple[StructureKind, ...]:
    """Which defined-risk structures express this view.

    Returns a set rather than a single choice: the bandit picks among these
    based on what has been working in the current regime. The edge test decides
    *what view to express*; the bandit decides *how*.
    """
    if verdict is EdgeVerdict.LONG_CONVEXITY:
        if direction == "up":
            return (StructureKind.CALL_DEBIT_SPREAD,)
        if direction == "down":
            return (StructureKind.PUT_DEBIT_SPREAD,)
        return (StructureKind.LONG_STRANGLE,)

    if verdict is EdgeVerdict.SHORT_PREMIUM:
        if direction == "up":
            return (StructureKind.BULL_PUT_SPREAD,)
        if direction == "down":
            return (StructureKind.BEAR_CALL_SPREAD,)
        return (StructureKind.IRON_CONDOR,)

    return ()


def evaluate_edge(
    expected_move_pct: float,
    direction: str,
    confidence: float,
    straddle_mid: float,
    spot: float,
    hours_to_expiry: float,
    horizon_hours: float,
    cfg,
) -> EdgeResult:
    """Compare the analyst's expected move against what the options imply.

    `horizon_hours` is retained for the audit record and for choosing an
    expiry, but deliberately does not scale the comparison — see
    `event_implied_move_pct` for why.
    """
    # The expiry we trade always spans the event, so the straddle is compared
    # directly rather than discounted to the analyst's horizon.
    implied = event_implied_move_pct(straddle_mid, spot)

    if confidence < cfg.signal.min_confidence:
        return EdgeResult(
            EdgeVerdict.NO_EDGE,
            expected_move_pct,
            implied,
            0.0,
            (),
            f"confidence {confidence:.2f} < {cfg.signal.min_confidence}",
        )

    if implied <= 0:
        return EdgeResult(
            EdgeVerdict.NO_EDGE,
            expected_move_pct,
            implied,
            0.0,
            (),
            "implied move is zero — unusable quote",
        )

    ratio = expected_move_pct / implied

    if ratio >= cfg.signal.max_plausible_ratio:
        # Options markets are not this wrong. A ratio this extreme means a bad
        # quote, a stale spot, or an analyst estimate detached from reality —
        # the same reasoning that rejects a credit above the spread width.
        return EdgeResult(
            EdgeVerdict.NO_EDGE,
            expected_move_pct,
            implied,
            ratio,
            (),
            f"ratio {ratio:.1f} exceeds plausible {cfg.signal.max_plausible_ratio} — "
            f"treating as bad data, not opportunity",
        )

    if ratio >= cfg.signal.edge_ratio_debit:
        verdict = EdgeVerdict.LONG_CONVEXITY
        detail = (
            f"expected {expected_move_pct:.2f}% vs implied {implied:.2f}% "
            f"(ratio {ratio:.2f}) — options underpricing this move"
        )
    elif ratio <= cfg.signal.edge_ratio_credit:
        verdict = EdgeVerdict.SHORT_PREMIUM
        detail = (
            f"expected {expected_move_pct:.2f}% vs implied {implied:.2f}% "
            f"(ratio {ratio:.2f}) — options overpricing this move"
        )
    else:
        verdict = EdgeVerdict.NO_EDGE
        detail = (
            f"expected {expected_move_pct:.2f}% vs implied {implied:.2f}% "
            f"(ratio {ratio:.2f}) — fairly priced, no edge"
        )

    return EdgeResult(
        verdict,
        expected_move_pct,
        implied,
        ratio,
        eligible_structures(verdict, direction),
        detail,
    )
