import math

import pytest

from glassbox.config import load_config
from glassbox.signal.edge import EdgeVerdict, evaluate_edge, implied_move_pct
from glassbox.structures import StructureKind

CFG = load_config()


def edge(expected, implied_straddle=5.0, spot=100.0, direction="up", confidence=0.8, **kw):
    return evaluate_edge(
        expected_move_pct=expected,
        direction=direction,
        confidence=confidence,
        straddle_mid=implied_straddle,
        spot=spot,
        hours_to_expiry=kw.get("hours_to_expiry", 168.0),
        horizon_hours=kw.get("horizon_hours", 168.0),
        cfg=CFG,
    )


# --- implied move maths ---------------------------------------------------


def test_implied_move_from_straddle():
    # $5 straddle on a $100 stock = 5% expected move to expiry
    assert implied_move_pct(5.0, 100.0, 168, 168) == pytest.approx(5.0)


def test_implied_move_scales_with_square_root_of_time():
    """A quarter of the time implies half the move, not a quarter."""
    full = implied_move_pct(5.0, 100.0, 168, 168)
    quarter = implied_move_pct(5.0, 100.0, 168, 42)
    assert quarter == pytest.approx(full * math.sqrt(0.25))
    assert quarter == pytest.approx(2.5)


def test_horizon_beyond_expiry_is_clamped():
    assert implied_move_pct(5.0, 100.0, 168, 999) == pytest.approx(5.0)


@pytest.mark.parametrize("bad", [(0.0, 100.0), (5.0, 0.0), (-1.0, 100.0)])
def test_invalid_quotes_rejected(bad):
    with pytest.raises(ValueError):
        implied_move_pct(bad[0], bad[1], 168, 168)


# --- the edge decision ----------------------------------------------------


def test_big_expected_move_versus_cheap_options_buys_convexity():
    r = edge(expected=10.0)  # implied 5% -> ratio 2.0
    assert r.verdict is EdgeVerdict.LONG_CONVEXITY and r.tradable
    assert "underpricing" in r.detail


def test_small_expected_move_versus_rich_options_sells_premium():
    r = edge(expected=2.0)  # implied 5% -> ratio 0.4
    assert r.verdict is EdgeVerdict.SHORT_PREMIUM and r.tradable
    assert "overpricing" in r.detail


def test_fairly_priced_news_is_not_traded():
    """Good news that is already priced in is not an opportunity."""
    r = edge(expected=5.0)  # ratio 1.0
    assert r.verdict is EdgeVerdict.NO_EDGE and not r.tradable
    assert r.eligible_structures == ()


def test_low_confidence_blocks_regardless_of_ratio():
    r = edge(expected=20.0, confidence=0.2)
    assert r.verdict is EdgeVerdict.NO_EDGE
    assert "confidence" in r.detail


# --- structure mapping ----------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "direction", "kind"),
    [
        (10.0, "up", StructureKind.CALL_DEBIT_SPREAD),
        (10.0, "down", StructureKind.PUT_DEBIT_SPREAD),
        (10.0, "vol_only", StructureKind.LONG_STRANGLE),
        (2.0, "up", StructureKind.BULL_PUT_SPREAD),
        (2.0, "down", StructureKind.BEAR_CALL_SPREAD),
        (2.0, "vol_only", StructureKind.IRON_CONDOR),
    ],
)
def test_view_maps_to_defined_risk_structures(expected, direction, kind):
    r = edge(expected=expected, direction=direction)
    assert kind in r.eligible_structures


def test_every_eligible_structure_is_defined_risk():
    """The edge test can never propose an undefined-risk expression of a view."""
    from glassbox.structures import CREDIT_STRUCTURES

    known = set(StructureKind)
    for expected in (10.0, 2.0):
        for direction in ("up", "down", "vol_only"):
            for kind in edge(expected=expected, direction=direction).eligible_structures:
                assert kind in known
                # credit structures only ever appear on the short-premium side
                if kind in CREDIT_STRUCTURES:
                    assert expected < 5.0


# --- jump vs diffusion ----------------------------------------------------


def test_event_comparison_is_unscaled():
    """News is a jump, not diffusion. The straddle on an expiry spanning the
    event already prices the jump, so it is compared directly."""
    from glassbox.signal.edge import event_implied_move_pct

    # $4 straddle on a $230 stock = the market pricing a ~1.74% move
    assert event_implied_move_pct(4.0, 230.0) == pytest.approx(1.739, abs=0.01)


def test_horizon_does_not_change_the_edge_verdict():
    """A jump does not accumulate over time, so the analyst's stated horizon
    must not alter how mispriced the option looks."""
    short = edge(expected=2.7, implied_straddle=4.0, spot=230.0, horizon_hours=4.0)
    long_ = edge(expected=2.7, implied_straddle=4.0, spot=230.0, horizon_hours=48.0)
    assert short.ratio == pytest.approx(long_.ratio)
    assert short.verdict is long_.verdict


def test_implausible_ratio_treated_as_bad_data():
    """An enormous edge is a data problem, not free money."""
    r = edge(expected=60.0, implied_straddle=1.0, spot=230.0)
    assert r.verdict is EdgeVerdict.NO_EDGE
    assert "bad data" in r.detail


def test_realistic_earnings_case_is_not_automatically_a_buy():
    """The live-run regression: a 2.7% expected move against a $4 straddle on a
    $230 stock should be judged on merit, not inflated by horizon scaling."""
    r = edge(
        expected=2.7, implied_straddle=4.0, spot=230.0, hours_to_expiry=168.0, horizon_hours=4.0
    )
    assert r.ratio < 10.0, f"horizon scaling still inflating the edge: {r.ratio:.1f}"
