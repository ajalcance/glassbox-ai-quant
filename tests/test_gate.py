"""Gate tests. The gate is a pure function, so we can enumerate its behaviour
exhaustively — including the property that matters most: no combination of
inputs approves an undefined-risk structure."""

from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from glassbox.config import load_config
from glassbox.gate import CHECKS, GateContext, evaluate
from glassbox.portfolio import Greeks, PortfolioState
from glassbox.structures import Right
from tests.conftest import make_bull_put, make_naked_put

CFG = load_config()
EXPIRY = date(2026, 9, 18)


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


def test_concentration_limits_come_from_config_not_literals(bull_put):
    """The position count, the correlated-position count and the correlation
    threshold itself were hardcoded in the gate — real risk limits with no
    config key, against invariant 6. Retuning the YAML must move the veto."""
    two_in_spy = PortfolioState(500.0, Greeks(), {"SPY": 2}, 2)
    assert not evaluate(ctx(structure=bull_put, portfolio=two_in_spy), CFG).approved

    roomier = CFG.model_copy(deep=True)
    roomier.gate.max_positions_per_underlying = 3
    d = evaluate(ctx(structure=bull_put, portfolio=two_in_spy), roomier)
    assert "concentration" not in veto_names(d), "raising the count must admit a third"

    # Correlation threshold: 0.80 pairs count as correlated at 0.7, not at 0.9.
    pair = PortfolioState(500.0, Greeks(), {"QQQ": 1, "IWM": 1}, 2)
    corr = {("QQQ", "SPY"): 0.80, ("IWM", "SPY"): 0.80}
    assert "correlation" in veto_names(evaluate(ctx(structure=bull_put, portfolio=pair,
                                                    correlations=corr), CFG))
    loose = CFG.model_copy(deep=True)
    loose.gate.correlation_threshold = 0.9
    d2 = evaluate(ctx(structure=bull_put, portfolio=pair, correlations=corr), loose)
    assert "correlation" not in veto_names(d2), "a higher bar must stop counting 0.80 as correlated"


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


def test_gate_refuses_entries_once_the_flatten_deadline_has_passed(bull_put):
    """A position must have somewhere to live. Past the flatten deadline the
    manager closes on the next tick, so approving an entry pays a round trip
    for nothing — the AAPL open-and-close-in-60s failure, systematised."""
    from datetime import UTC, datetime, timedelta

    deadline = datetime(2026, 9, 4, 15, 55, tzinfo=UTC)
    dated = CFG.model_copy(update={
        "manage": CFG.manage.model_copy(update={"flatten_all_at": deadline.isoformat()})
    })
    after = evaluate(ctx(structure=bull_put, now=deadline + timedelta(minutes=1)), dated)
    assert "flatten_deadline" in veto_names(after)

    before = evaluate(ctx(structure=bull_put, now=deadline - timedelta(hours=1)), dated)
    assert "flatten_deadline" not in veto_names(before)

    # no clock supplied must not veto — time checks pass rather than guess
    assert "flatten_deadline" not in veto_names(evaluate(ctx(structure=bull_put), dated))
    # and no deadline configured (the standing default) never vetoes
    assert "flatten_deadline" not in veto_names(
        evaluate(ctx(structure=bull_put, now=deadline + timedelta(days=30)), CFG)
    )
    assert UTC  # silence unused-import pedantry


# -- leg overlap ---------------------------------------------------------------
# `duplicate` compares whole-structure identity, which is blind to two spreads
# that share one contract. The broker is not blind to it: it nets by contract.


def test_opposite_side_of_a_held_leg_is_refused(bull_put):
    """The exact rejection Alpaca returned three times, most recently 11 Sep:
    an ORCL 150/148 put spread proposed while we were short that 148 put —
    `position intent mismatch, inferred: buy_to_close, specified: buy_to_open`.
    An order the broker will always reject should never leave the building."""
    held = {"SPY260918P00435000": "short"}  # we hold the long leg, short
    d = evaluate(ctx(structure=bull_put, held_legs=held), CFG)
    assert not d.approved and "leg_conflict" in veto_names(d)
    assert "opposite side" in next(c for c in d.vetoes if c.name == "leg_conflict").detail


def test_same_side_of_a_held_leg_is_refused(bull_put):
    """9 Sep: an AAPL iron condor and a bull put spread that was exactly its put
    wing ran side by side. No limit breached, but the heat number understated
    what was actually on, because position-level accounting counted two
    unrelated positions where one strike carried double."""
    held = {"SPY260918P00440000": "short"}
    d = evaluate(ctx(structure=bull_put, held_legs=held), CFG)
    assert not d.approved and "leg_conflict" in veto_names(d)
    assert "already held" in next(c for c in d.vetoes if c.name == "leg_conflict").detail


def test_unrelated_held_legs_do_not_block(bull_put):
    """Two spreads on the same underlying at different strikes remain fine."""
    held = {"SPY260918P00400000": "short", "AAPL260918C00230000": "long"}
    d = evaluate(ctx(structure=bull_put, held_legs=held), CFG)
    assert d.approved, [c.detail for c in d.vetoes]


def test_no_held_legs_passes(bull_put):
    d = evaluate(ctx(structure=bull_put, held_legs={}), CFG)
    assert d.approved and "leg_conflict" not in veto_names(d)


# -- cost to trade -------------------------------------------------------------
# Every other check asks whether a trade is too risky. This one asks whether it
# can make money at all.


def test_target_below_the_round_trip_cost_is_refused(bull_put):
    """11 Sep, ORCL 148/146: $52 credit, $26 target, ~$21 to get on and off.
    Marked -$21 on its first tick, never traded above -$7 in 56 minutes."""
    d = evaluate(ctx(structure=bull_put, entry_price=-0.52, round_trip_cost=21.0), CFG)
    assert not d.approved and "cost_to_trade" in veto_names(d)
    assert "no path to profit" in next(c for c in d.vetoes if c.name == "cost_to_trade").detail


def test_comfortable_credit_passes(bull_put):
    """ORCL 155/152.5 the same morning: $105 credit, $52 target, ~$26 cost."""
    d = evaluate(ctx(structure=bull_put, entry_price=-1.05, round_trip_cost=26.0), CFG)
    assert d.approved, [c.detail for c in d.vetoes]


def test_debit_structures_use_their_own_take_profit(bull_put):
    """QQQ 715/705: $211 debit, 100% target, ~$14 cost — 15x clear."""
    d = evaluate(ctx(structure=bull_put, entry_price=2.11, round_trip_cost=14.0), CFG)
    assert d.approved, [c.detail for c in d.vetoes]


def test_unpriceable_cost_is_not_treated_as_free(bull_put):
    """A zero cost means the quotes could not price the round trip. The
    liquidity check owns that decision; this one must not duplicate it."""
    d = evaluate(ctx(structure=bull_put, entry_price=-0.52, round_trip_cost=0.0), CFG)
    result = next(c for c in d.checks if c.name == "cost_to_trade")
    assert result.passed and "not priceable" in result.detail


def test_round_trip_cost_sums_leg_spreads():
    from glassbox.chain import ContractQuote, structure_round_trip_cost
    from tests.conftest import make_bull_put

    s = make_bull_put()
    chain = [
        ContractQuote("SPY260918P00440000", Right.PUT, 440, EXPIRY, bid=2.30, ask=2.45),
        ContractQuote("SPY260918P00435000", Right.PUT, 435, EXPIRY, bid=1.10, ask=1.16),
    ]
    assert structure_round_trip_cost(s, chain) == pytest.approx(0.15 * 100 + 0.06 * 100)


def test_round_trip_cost_is_unknown_when_a_leg_is_missing():
    """An unpriced leg must not report the structure as cheaper than it is."""
    from glassbox.chain import ContractQuote, structure_round_trip_cost
    from tests.conftest import make_bull_put

    chain = [
        ContractQuote("SPY260918P00440000", Right.PUT, 440, EXPIRY, bid=2.30, ask=2.45)
    ]
    assert structure_round_trip_cost(make_bull_put(), chain) == 0.0
