"""Gate tests. The gate is a pure function, so we can enumerate its behaviour
exhaustively — including the property that matters most: no combination of
inputs approves an undefined-risk structure."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from glassbox.config import load_config
from glassbox.gate import CHECKS, GateContext, evaluate
from glassbox.portfolio import Greeks, PortfolioState
from tests.conftest import make_bull_put, make_naked_put

CFG = load_config()


def ctx(**overrides) -> GateContext:
    """A proposal that passes every check, so each test changes exactly one thing."""
    base = {
        "structure": None,
        "qty": 1,
        "max_loss_per_spread": 380.0,
        "meta_label_p": 0.70,
        "equity": 100_000.0,
        "daily_pnl_pct": 0.0,
        "drawdown_pct": 0.0,
        "market_open": True,
        "minutes_since_open": 60,
        "minutes_to_close": 120,
        "hours_to_expiry": 200.0,
        "halted": False,
        "kill_switch": False,
        "portfolio": PortfolioState(0.0, Greeks(), {}, 0),
        "post_trade_greeks": Greeks(delta_dollars=2_000),
        "spread_pct_of_mid": 1.5,
        "open_interest": 5_000,
        "orders_last_minute": 0,
        "new_positions_today": 0,
        "duplicate_open": False,
    }
    base.update(overrides)
    return GateContext(**base)


def veto_names(decision):
    return {c.name for c in decision.vetoes}


def test_clean_proposal_approved(bull_put):
    d = evaluate(ctx(structure=bull_put), CFG)
    assert d.approved, d.reason
    assert len(d.checks) == len(CHECKS)


def test_every_check_is_recorded_including_passes(bull_put):
    d = evaluate(ctx(structure=bull_put), CFG)
    assert all(c.detail for c in d.checks), "every check must explain itself"
    assert len(d.as_dict()["checks"]) == len(CHECKS)


# --- categorical blocks ---------------------------------------------------


def test_kill_switch_blocks(bull_put):
    d = evaluate(ctx(structure=bull_put, kill_switch=True), CFG)
    assert not d.approved and "kill_switch" in veto_names(d)


def test_halt_blocks(bull_put):
    d = evaluate(ctx(structure=bull_put, halted=True), CFG)
    assert not d.approved and "system_halted" in veto_names(d)


def test_naked_structure_never_approved(naked_put):
    d = evaluate(ctx(structure=naked_put), CFG)
    assert not d.approved and "defined_risk" in veto_names(d)


# --- risk limits ----------------------------------------------------------


def test_position_larger_than_cap_blocked(bull_put):
    # 1.5% of 100k = $1500 cap; 5 x $380 = $1900
    d = evaluate(ctx(structure=bull_put, qty=5), CFG)
    assert not d.approved and "position_size" in veto_names(d)


def test_heat_cap_blocks_when_book_is_full(bull_put):
    # 6% of 100k = $6000 cap; already at $5800
    full = PortfolioState(5_800.0, Greeks(), {"QQQ": 1}, 1)
    d = evaluate(ctx(structure=bull_put, portfolio=full), CFG)
    assert not d.approved and "portfolio_heat" in veto_names(d)


def test_delta_band_blocks(bull_put):
    over = CFG.risk.delta_dollars_band + 1_000  # relative to config: the band
    # is a calibration number and the test must not re-pin its value
    d = evaluate(ctx(structure=bull_put, post_trade_greeks=Greeks(delta_dollars=over)), CFG)
    assert not d.approved and "greeks_bands" in veto_names(d)
    d2 = evaluate(ctx(structure=bull_put, post_trade_greeks=Greeks(delta_dollars=-over)), CFG)
    assert not d2.approved, "band must be symmetric"


def test_third_position_in_same_underlying_blocked(bull_put):
    p = PortfolioState(500.0, Greeks(), {"SPY": 2}, 2)
    d = evaluate(ctx(structure=bull_put, portfolio=p), CFG)
    assert not d.approved and "concentration" in veto_names(d)


def test_correlated_names_count_as_concentration(bull_put):
    p = PortfolioState(500.0, Greeks(), {"QQQ": 1, "IWM": 1}, 2)
    corr = {("QQQ", "SPY"): 0.95, ("IWM", "SPY"): 0.88}
    d = evaluate(ctx(structure=bull_put, portfolio=p, correlations=corr), CFG)
    assert not d.approved and "correlation" in veto_names(d)


def test_uncorrelated_names_do_not_block(bull_put):
    p = PortfolioState(500.0, Greeks(), {"TLT": 1, "GLD": 1}, 2)
    corr = {("SPY", "TLT"): -0.2, ("GLD", "SPY"): 0.1}
    d = evaluate(ctx(structure=bull_put, portfolio=p, correlations=corr), CFG)
    assert d.approved, d.reason


# --- session and liquidity ------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"market_open": False},
        {"minutes_since_open": 2},  # opening auction
        {"minutes_to_close": 3},  # closing auction
    ],
)
def test_market_window_blocks(bull_put, kw):
    d = evaluate(ctx(structure=bull_put, **kw), CFG)
    assert not d.approved and "market_window" in veto_names(d)


@pytest.mark.parametrize("kw", [{"spread_pct_of_mid": 25.0}, {"open_interest": 5}])
def test_illiquid_blocked(bull_put, kw):
    d = evaluate(ctx(structure=bull_put, **kw), CFG)
    assert not d.approved and "liquidity" in veto_names(d)


def test_near_expiry_blocked(bull_put):
    d = evaluate(ctx(structure=bull_put, hours_to_expiry=6.0), CFG)
    assert not d.approved and "time_to_expiry" in veto_names(d)


# --- account guards mirrored in the gate ----------------------------------


def test_daily_loss_and_drawdown_block(bull_put):
    d = evaluate(ctx(structure=bull_put, daily_pnl_pct=-2.5), CFG)
    assert not d.approved and "daily_loss" in veto_names(d)
    d2 = evaluate(ctx(structure=bull_put, drawdown_pct=-7.0), CFG)
    assert not d2.approved and "max_drawdown" in veto_names(d2)


@pytest.mark.parametrize("kw", [{"orders_last_minute": 5}, {"new_positions_today": 10}])
def test_rate_limits_block(bull_put, kw):
    d = evaluate(ctx(structure=bull_put, **kw), CFG)
    assert not d.approved and "rate_limit" in veto_names(d)


def test_duplicate_blocked(bull_put):
    d = evaluate(ctx(structure=bull_put, duplicate_open=True), CFG)
    assert not d.approved and "duplicate" in veto_names(d)


def test_multiple_vetoes_all_reported(bull_put):
    """No short-circuit: the veto log should show every reason, not just one."""
    d = evaluate(ctx(structure=bull_put, halted=True, qty=99, open_interest=1), CFG)
    assert {"system_halted", "position_size", "liquidity"} <= veto_names(d)


# --- property: the invariant holds across the whole input space -----------


@settings(max_examples=200, deadline=None)
@given(
    qty=st.integers(min_value=-5, max_value=50),
    equity=st.floats(min_value=1_000, max_value=1_000_000),
    heat=st.floats(min_value=0, max_value=50_000),
    delta=st.floats(min_value=-100_000, max_value=100_000),
    daily=st.floats(min_value=-20, max_value=20),
)
def test_property_naked_never_approved(qty, equity, heat, delta, daily):
    d = evaluate(
        ctx(
            structure=make_naked_put(),
            qty=qty,
            equity=equity,
            portfolio=PortfolioState(heat, Greeks(), {}, 1),
            post_trade_greeks=Greeks(delta_dollars=delta),
            daily_pnl_pct=daily,
        ),
        CFG,
    )
    assert not d.approved, "an undefined-risk structure was approved"


@settings(max_examples=200, deadline=None)
@given(
    qty=st.integers(min_value=1, max_value=100),
    max_loss=st.floats(min_value=1, max_value=5_000),
    heat=st.floats(min_value=0, max_value=20_000),
)
def test_property_approved_never_exceeds_heat_cap(qty, max_loss, heat):
    equity = 100_000.0
    d = evaluate(
        ctx(
            structure=make_bull_put(),
            qty=qty,
            max_loss_per_spread=max_loss,
            equity=equity,
            portfolio=PortfolioState(heat, Greeks(), {}, 1),
        ),
        CFG,
    )
    if d.approved:
        projected = heat + qty * max_loss
        assert projected <= equity * (CFG.risk.portfolio_heat_pct / 100) + 1e-6
        assert qty * max_loss <= equity * (CFG.risk.max_loss_per_position_pct / 100) + 1e-6


# --- session room ---------------------------------------------------------


def test_intraday_thesis_needs_room_before_the_close(bull_put):
    """A four-hour thesis entered with forty minutes left cannot resolve."""
    d = evaluate(ctx(structure=bull_put, horizon_hours=4.0, minutes_to_close=40), CFG)
    assert not d.approved and "session_room" in veto_names(d)


def test_intraday_thesis_with_enough_room_passes(bull_put):
    # 4h thesis needs 120m at min_session_fraction 0.5
    d = evaluate(ctx(structure=bull_put, horizon_hours=4.0, minutes_to_close=150), CFG)
    assert d.approved, d.reason


def test_boundary_is_exactly_half_the_horizon(bull_put):
    tight = evaluate(ctx(structure=bull_put, horizon_hours=4.0, minutes_to_close=119), CFG)
    ok = evaluate(ctx(structure=bull_put, horizon_hours=4.0, minutes_to_close=120), CFG)
    assert "session_room" in veto_names(tight)
    assert "session_room" not in veto_names(ok)


def test_multi_day_thesis_is_exempt(bull_put):
    """Spanning sessions is the point of a multi-day view."""
    d = evaluate(ctx(structure=bull_put, horizon_hours=48.0, minutes_to_close=30), CFG)
    assert "session_room" not in veto_names(d)


def test_no_horizon_supplied_does_not_block(bull_put):
    d = evaluate(ctx(structure=bull_put, horizon_hours=0.0), CFG)
    assert "session_room" not in veto_names(d)


def test_widened_session_edges_are_enforced(bull_put):
    """The first and last stretches carry wide spreads and unstable IV."""
    early = evaluate(ctx(structure=bull_put, minutes_since_open=10), CFG)
    late = evaluate(ctx(structure=bull_put, minutes_to_close=15), CFG)
    assert "market_window" in veto_names(early)
    assert "market_window" in veto_names(late)
