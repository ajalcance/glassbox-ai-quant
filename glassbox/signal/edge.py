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
    expected_move_pct: float  # after discounting what already moved
    implied_move_pct: float
    ratio: float
    eligible_structures: tuple[StructureKind, ...]
    detail: str
    raw_expected_move_pct: float = 0.0  # the analyst's estimate before discount
    realized_move_pct: float = 0.0  # already spent since the story broke
    vrp_ratio: float | None = None  # implied / forecast realized

    @property
    def tradable(self) -> bool:
        return self.verdict is not EdgeVerdict.NO_EDGE


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


def remaining_move(expected_pct: float, realized_pct: float | None) -> float:
    """Expected move less whatever the market has already made.

    An "expected 5% vs implied 3%" signal on a name that has already run 5% is
    really "0% remaining vs 3%" — the same headline, the opposite conclusion.
    An unmeasurable reaction discounts nothing rather than assuming none.
    """
    if realized_pct is None:
        return expected_pct
    return max(0.0, expected_pct - realized_pct)


def vrp_permits(
    verdict: EdgeVerdict, vrp_ratio: float | None, cfg, vrp_shift: float = 0.0
) -> tuple[bool, str]:
    """Does the volatility risk premium agree with the direction we chose?

    The expected-vs-implied test says the market has mispriced *this event*.
    This asks a different question: are these options expensive relative to what
    the underlying actually delivers? Buying rich optionality is the expensive
    mistake, so the ceiling on debits is the tighter of the two bounds.

    An unavailable forecast permits the trade — the volatility model is a second
    opinion, not a precondition.
    """
    if vrp_ratio is None:
        return True, "no volatility forecast"
    # Stress shifts both bounds toward selling: premium is genuinely richer, so
    # selling is better paid and buying is more expensive. Calm shifts back.
    debit_ceiling = cfg.signal.vrp_max_for_debit - max(vrp_shift, 0.0)
    credit_floor = cfg.signal.vrp_min_for_credit - vrp_shift
    if verdict is EdgeVerdict.LONG_CONVEXITY and vrp_ratio > debit_ceiling:
        return False, (
            f"VRP {vrp_ratio:.2f} > {debit_ceiling:.2f} — options already "
            f"price more movement than this name delivers; buying them is the "
            f"expensive side"
        )
    if verdict is EdgeVerdict.SHORT_PREMIUM and vrp_ratio < credit_floor:
        return False, (
            f"VRP {vrp_ratio:.2f} < {credit_floor:.2f} — premium is thin "
            f"relative to realised movement; not paid enough to be short"
        )
    return True, f"VRP {vrp_ratio:.2f} agrees"


def evaluate_edge(
    expected_move_pct: float,
    direction: str,
    confidence: float,
    straddle_mid: float,
    spot: float,
    hours_to_expiry: float,
    horizon_hours: float,
    cfg,
    realized_move_pct: float | None = None,
    forecast_move_pct: float | None = None,
    vrp_shift: float = 0.0,
    relax_band: bool = False,
) -> EdgeResult:
    """Compare the analyst's expected move against what the options imply.

    `horizon_hours` is retained for the audit record and for choosing an
    expiry, but deliberately does not scale the comparison — see
    `event_implied_move_pct` for why.
    """
    # The expiry we trade always spans the event, so the straddle is compared
    # directly rather than discounted to the analyst's horizon.
    implied = event_implied_move_pct(straddle_mid, spot)
    raw_expected = expected_move_pct
    consumed = realized_move_pct if cfg.signal.consume_realized_move else None
    expected_move_pct = remaining_move(raw_expected, consumed)
    vrp = (implied / forecast_move_pct) if forecast_move_pct else None

    def result(verdict, ratio, detail, structures=()):
        return EdgeResult(
            verdict,
            expected_move_pct,
            implied,
            ratio,
            structures,
            detail,
            raw_expected_move_pct=raw_expected,
            realized_move_pct=consumed or 0.0,
            vrp_ratio=vrp,
        )

    if confidence < cfg.signal.min_confidence:
        return result(
            EdgeVerdict.NO_EDGE,
            0.0,
            f"confidence {confidence:.2f} < {cfg.signal.min_confidence}",
        )

    if consumed and expected_move_pct <= 0:
        return result(
            EdgeVerdict.NO_EDGE,
            0.0,
            f"expected {raw_expected:.2f}% already moved {consumed:.2f}% — "
            f"nothing left of the thesis",
        )

    if implied <= 0:
        return result(EdgeVerdict.NO_EDGE, 0.0, "implied move is zero — unusable quote")

    ratio = expected_move_pct / implied

    if ratio >= cfg.signal.max_plausible_ratio:
        # Options markets are not this wrong. A ratio this extreme means a bad
        # quote, a stale spot, or an analyst estimate detached from reality —
        # the same reasoning that rejects a credit above the spread width.
        return result(
            EdgeVerdict.NO_EDGE,
            ratio,
            f"ratio {ratio:.1f} exceeds plausible {cfg.signal.max_plausible_ratio} — "
            f"treating as bad data, not opportunity",
        )

    debit_bar = 1.0 if relax_band else cfg.signal.edge_ratio_debit
    credit_bar = 1.0 if relax_band else cfg.signal.edge_ratio_credit
    if ratio >= debit_bar and ratio > 1.0:
        verdict = EdgeVerdict.LONG_CONVEXITY
        detail = (
            f"expected {expected_move_pct:.2f}% vs implied {implied:.2f}% "
            f"(ratio {ratio:.2f}) — options underpricing this move"
        )
    elif ratio <= credit_bar and ratio < 1.0:
        verdict = EdgeVerdict.SHORT_PREMIUM
        detail = (
            f"expected {expected_move_pct:.2f}% vs implied {implied:.2f}% "
            f"(ratio {ratio:.2f}) — options overpricing this move"
        )
    else:
        return result(
            EdgeVerdict.NO_EDGE,
            ratio,
            f"expected {expected_move_pct:.2f}% vs implied {implied:.2f}% "
            f"(ratio {ratio:.2f}) — fairly priced, no edge",
        )

    permitted, vrp_detail = vrp_permits(verdict, vrp, cfg, vrp_shift)
    if not permitted:
        return result(EdgeVerdict.NO_EDGE, ratio, vrp_detail)

    if consumed:
        detail += f"; {consumed:.2f}% of {raw_expected:.2f}% already moved"
    return result(
        verdict, ratio, f"{detail}; {vrp_detail}", eligible_structures(verdict, direction)
    )
