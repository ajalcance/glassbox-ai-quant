import pytest

from glassbox.config import load_config
from glassbox.sizing import meta_multiplier, size_position

CFG = load_config()


def test_below_threshold_means_no_trade():
    assert meta_multiplier(0.50, CFG) == 0.0
    r = size_position(100_000, 380, meta_label_p=0.50, cfg=CFG)
    assert not r.approved and "below" in r.reason


def test_multiplier_scales_and_is_capped():
    assert meta_multiplier(0.55, CFG) == pytest.approx(CFG.sizing.meta_multiplier_floor)
    assert meta_multiplier(0.85, CFG) == CFG.sizing.meta_multiplier_ceiling
    assert meta_multiplier(0.99, CFG) == CFG.sizing.meta_multiplier_ceiling, "never lever up"
    assert meta_multiplier(0.70, CFG) > meta_multiplier(0.60, CFG), "monotonic"


def test_sizing_respects_r_budget():
    # R = 0.5% of 100k = $500, multiplier 1.0 at p>=0.85 -> 1 spread at $380
    r = size_position(100_000, 380, meta_label_p=0.90, cfg=CFG)
    assert r.approved and r.qty == 1
    assert r.r_dollars == pytest.approx(500.0)


def test_smaller_of_two_budgets_wins():
    """High vol shrinks the position even when the R budget would allow more."""
    calm = size_position(100_000, 100, 0.90, CFG, underlying_vol=0.005)
    wild = size_position(100_000, 100, 0.90, CFG, underlying_vol=0.05)
    assert wild.qty < calm.qty
    assert wild.qty == min(wild.fixed_fractional_qty, wild.vol_target_qty)


def test_loss_streak_halves_risk():
    normal = size_position(100_000, 100, 0.90, CFG)
    streaked = size_position(100_000, 100, 0.90, CFG, loss_streak=4)
    assert streaked.qty < normal.qty, "must reduce after losses, never increase"
    assert streaked.r_dollars == pytest.approx(normal.r_dollars / 2)


def test_never_exceeds_per_position_cap():
    """Even an enormous R budget cannot breach the 1.5% position ceiling."""
    r = size_position(100_000, 10, 0.99, CFG)
    assert r.qty * 10 <= 100_000 * CFG.risk.max_loss_per_position_pct / 100


def test_expensive_spread_rejected_not_rounded_up():
    r = size_position(100_000, 5_000, 0.90, CFG)
    assert not r.approved and "budget" in r.reason


def test_zero_max_loss_refused():
    """The bug the live run exposed: a zero-risk spread must never size."""
    with pytest.raises(ValueError, match="must be positive"):
        size_position(100_000, 0.0, 0.90, CFG)


# --- budget tapers ---------------------------------------------------------

from glassbox.sizing import drawdown_taper, heat_taper

EQUITY = 100_000.0
CAP = EQUITY * CFG.risk.portfolio_heat_pct / 100


def test_heat_taper_is_inert_while_there_is_room():
    assert heat_taper(0.0, EQUITY, CFG) == 1.0
    assert heat_taper(CAP * 0.5, EQUITY, CFG) == 1.0


def test_heat_taper_shrinks_toward_the_cap():
    """The cap was a tripwire: full conviction at 5.9% of a 6% budget and
    nothing at 6.1%. A portfolio near its budget should take smaller bites."""
    mid = heat_taper(CAP * 0.8, EQUITY, CFG)
    near = heat_taper(CAP * 0.95, EQUITY, CFG)
    assert 1.0 > mid > near >= CFG.sizing.heat_taper_floor


def test_heat_taper_is_monotonic_and_floored():
    previous = 1.0
    for fraction in (0.6, 0.7, 0.8, 0.9, 1.0, 1.5):
        value = heat_taper(CAP * fraction, EQUITY, CFG)
        assert value <= previous
        previous = value
    assert previous == pytest.approx(CFG.sizing.heat_taper_floor)


def test_drawdown_taper_ignores_a_profitable_day():
    assert drawdown_taper(0.0, CFG) == 1.0
    assert drawdown_taper(1.5, CFG) == 1.0


def test_drawdown_taper_shrinks_toward_the_halt():
    limit = CFG.risk.daily_loss_halt_pct
    assert drawdown_taper(-limit * 0.4, CFG) == 1.0
    shrinking = drawdown_taper(-limit * 0.75, CFG)
    nearly = drawdown_taper(-limit * 0.95, CFG)
    assert 1.0 > shrinking > nearly >= CFG.sizing.drawdown_taper_floor


def test_tapers_never_exceed_one():
    """A budget with room to spare is not a reason to take a larger position
    than the rules already permit."""
    assert heat_taper(-500.0, EQUITY, CFG) <= 1.0
    assert drawdown_taper(-0.0001, CFG) <= 1.0


def test_taper_reduces_actual_contracts():
    full = size_position(EQUITY, 100.0, 0.9, CFG, context_multiplier=1.0)
    tapered = size_position(
        EQUITY,
        100.0,
        0.9,
        CFG,
        context_multiplier=heat_taper(CAP * 0.95, EQUITY, CFG),
    )
    assert tapered.qty < full.qty


def test_zero_equity_does_not_divide_by_zero():
    assert heat_taper(0.0, 0.0, CFG) == 1.0
