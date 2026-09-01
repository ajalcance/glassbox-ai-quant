"""Trade manager tests.

The barrier that closes a position also labels it, so these tests are as much
about the training signal as about the exits.
"""

from datetime import UTC, datetime, timedelta

import pytest

from glassbox.config import load_config
from glassbox.manage import (
    Action,
    Barrier,
    PositionView,
    evaluate_position,
    profit_target,
    stop_level,
)
from glassbox.structures import StructureKind

CFG = load_config()
OPENED = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
LATER = OPENED + timedelta(hours=2)


def credit_view(current_price=-1.20, **kw):
    """Sold a 5-wide put spread for 1.20; max loss 380/spread."""
    base = {
        "position_id": "pos-1",
        "kind": StructureKind.BULL_PUT_SPREAD,
        "qty": 1,
        "entry_price": -1.20,
        "current_price": current_price,
        "max_loss_per_spread": 380.0,
        "opened_at": OPENED,
        "horizon_hours": 24.0,
        "hours_to_expiry": 168.0,
    }
    base.update(kw)
    return PositionView(**base)


def debit_view(current_price=2.00, **kw):
    """Paid 2.00 for a call spread; max loss 200/spread."""
    base = {
        "position_id": "pos-2",
        "kind": StructureKind.CALL_DEBIT_SPREAD,
        "qty": 1,
        "entry_price": 2.00,
        "current_price": current_price,
        "max_loss_per_spread": 200.0,
        "opened_at": OPENED,
        "horizon_hours": 24.0,
        "hours_to_expiry": 168.0,
    }
    base.update(kw)
    return PositionView(**base)


# --- P&L sign conventions -------------------------------------------------


def test_credit_position_profits_as_spread_cheapens():
    assert credit_view(current_price=-0.60).unrealized_pnl == pytest.approx(60.0)
    assert credit_view(current_price=-2.00).unrealized_pnl == pytest.approx(-80.0)
    assert credit_view(current_price=-1.20).unrealized_pnl == pytest.approx(0.0)


def test_debit_position_profits_as_spread_richens():
    assert debit_view(current_price=3.00).unrealized_pnl == pytest.approx(100.0)
    assert debit_view(current_price=1.00).unrealized_pnl == pytest.approx(-100.0)


def test_pnl_scales_with_quantity():
    assert credit_view(current_price=-0.60, qty=5).unrealized_pnl == pytest.approx(300.0)


# --- barrier levels -------------------------------------------------------


def test_credit_target_is_half_the_premium():
    assert profit_target(credit_view(), CFG) == pytest.approx(60.0)  # 50% of $120


def test_credit_stop_is_one_and_a_half_times_the_credit():
    # tightened 2.0x -> 1.5x on 1 Sep, paired with the overnight-carry policy
    assert stop_level(credit_view(), CFG) == pytest.approx(-180.0)


def test_stop_never_exceeds_defined_max_loss():
    """A stop wider than the worst case is not a stop."""
    thin = credit_view(entry_price=-3.00, max_loss_per_spread=200.0)
    assert stop_level(thin, CFG) == pytest.approx(-200.0)  # not -600


def test_debit_target_and_stop():
    assert profit_target(debit_view(), CFG) == pytest.approx(200.0)  # +100% of debit
    assert stop_level(debit_view(), CFG) == pytest.approx(-80.0)  # -40% of debit


# --- barriers -------------------------------------------------------------


def test_holds_between_barriers():
    d = evaluate_position(credit_view(current_price=-1.00), CFG, LATER)
    assert d.action is Action.HOLD and d.barrier is Barrier.NONE
    assert d.label is None, "an open position has no label yet"


def test_profit_barrier_closes_and_labels_win():
    d = evaluate_position(credit_view(current_price=-0.55), CFG, LATER)
    assert d.should_close and d.barrier is Barrier.PROFIT and d.label == 1


def test_stop_barrier_closes_and_labels_loss():
    d = evaluate_position(credit_view(current_price=-3.70), CFG, LATER)
    assert d.should_close and d.barrier is Barrier.STOP and d.label == 0


def test_time_barrier_banks_winners_only():
    """The bell banks winners; it never realises losers (1 Sep policy).

    A thesis past its horizon in profit is banked. Past its horizon at a
    loss it RIDES — the wing bounds the downside, and the stop, macro_risk,
    expiry_risk and deadline barriers all still apply."""
    expired = OPENED + timedelta(hours=25)
    win = evaluate_position(credit_view(current_price=-1.00), CFG, expired)
    assert win.barrier is Barrier.TIME and win.label == 1

    lose = evaluate_position(credit_view(current_price=-1.50), CFG, expired)
    assert lose.action is Action.HOLD, "an expired loser rides, bounded by the wing"
    assert "riding" in lose.reason


def test_expired_loser_still_stops_out():
    """Riding a loss past the horizon is NOT riding it to max loss: the
    tightened stop still cuts it."""
    expired = OPENED + timedelta(hours=25)
    d = evaluate_position(credit_view(current_price=-3.20), CFG, expired)
    assert d.should_close and d.barrier is Barrier.STOP


def test_expired_loser_still_bounded_by_deadline():
    expired = OPENED + timedelta(hours=25)
    d = evaluate_position(credit_view(current_price=-1.50), CFG, expired, deadline=OPENED)
    assert d.should_close and d.barrier is Barrier.DEADLINE


# --- break-even -----------------------------------------------------------


def test_breakeven_stops_a_winner_becoming_a_loser():
    # target is $60; peaked at $33 (55% — above the break-even trigger, below
    # the trail arm), now back to flat: the zero floor is the active layer
    d = evaluate_position(credit_view(current_price=-1.20, peak_pnl=33.0), CFG, LATER)
    assert d.should_close and d.barrier is Barrier.BREAKEVEN


# --- trailing lock -----------------------------------------------------------


def test_trail_locks_half_the_peak_once_armed():
    """TLT, 1 Sep: +$306 gave back $170 with nothing banking any of it. Once
    the peak clears trail_arm_pct of target, falling to trail_keep_pct of
    the peak closes — a winner keeps most of what it earned."""
    # target $60; peaked at $50 (83%) -> armed, floor $25; now +$20
    d = evaluate_position(credit_view(current_price=-1.00, peak_pnl=50.0), CFG, LATER)
    assert d.should_close and d.barrier is Barrier.TRAIL and d.label == 1
    assert "$+25" in d.reason


def test_trail_holds_above_its_floor():
    # peaked $50, floor $25, now +$40: still running
    d = evaluate_position(credit_view(current_price=-0.80, peak_pnl=50.0), CFG, LATER)
    assert d.action is Action.HOLD
    assert "trail armed" in d.reason


def test_trail_not_armed_below_arm_threshold():
    # peaked $30 (50% of target): break-even armed, trail not; now +$10 holds
    d = evaluate_position(credit_view(current_price=-1.10, peak_pnl=30.0), CFG, LATER)
    assert d.action is Action.HOLD and "trail armed" not in d.reason


def test_trail_outranks_breakeven_but_not_stop_or_profit():
    # profit target still wins when hit outright
    d = evaluate_position(credit_view(current_price=-0.55, peak_pnl=65.0), CFG, LATER)
    assert d.barrier is Barrier.PROFIT
    # a stop-level loss is a STOP, whatever the peak was
    d = evaluate_position(credit_view(current_price=-3.20, peak_pnl=50.0), CFG, LATER)
    assert d.barrier is Barrier.STOP


def test_breakeven_not_armed_before_trigger():
    d = evaluate_position(credit_view(current_price=-1.20, peak_pnl=10.0), CFG, LATER)
    assert d.action is Action.HOLD


def test_breakeven_does_not_fire_while_still_profitable():
    d = evaluate_position(credit_view(current_price=-0.90, peak_pnl=50.0), CFG, LATER)
    assert d.action is Action.HOLD
    assert "break-even armed" in d.reason


# --- obligations outrank opportunities ------------------------------------


def test_deadline_closes_even_a_winning_position():
    """A position must never survive the cutoff because it happened to be up."""
    deadline = OPENED + timedelta(hours=1)
    d = evaluate_position(credit_view(current_price=-0.10), CFG, LATER, deadline=deadline)
    assert d.should_close and d.barrier is Barrier.DEADLINE


def test_deadline_outranks_stop():
    deadline = OPENED + timedelta(hours=1)
    d = evaluate_position(credit_view(current_price=-5.00), CFG, LATER, deadline=deadline)
    assert d.barrier is Barrier.DEADLINE


def test_near_expiry_closes_regardless_of_pnl():
    """Gamma accelerates into expiry; we leave before it does."""
    for price in (-0.10, -1.20, -3.00):
        d = evaluate_position(credit_view(current_price=price, hours_to_expiry=6.0), CFG, LATER)
        assert d.should_close and d.barrier is Barrier.EXPIRY_RISK


def test_stop_outranks_target_when_both_somehow_true():
    """Severity ordering: risk before reward."""
    v = credit_view(current_price=-3.70, peak_pnl=100.0)
    assert evaluate_position(v, CFG, LATER).barrier is Barrier.STOP


# --- labelling contract ---------------------------------------------------


def test_every_close_produces_a_label():
    """The exit is the training signal, so no close may be unlabelled."""
    cases = [
        credit_view(current_price=-0.55),  # profit
        credit_view(current_price=-3.70),  # stop
        credit_view(current_price=-1.20, hours_to_expiry=2.0),  # expiry
        credit_view(current_price=-1.20, peak_pnl=50.0),  # breakeven
    ]
    for v in cases:
        d = evaluate_position(v, CFG, LATER)
        assert d.should_close and d.label in (0, 1), f"unlabelled close: {d.barrier}"


# --- macro exit -----------------------------------------------------------


class FakeMacroWindow:
    def __init__(self, active=True, detail="Nonfarm Payrolls in 90m"):
        self.active = active
        self.detail = detail


def test_short_premium_is_closed_before_a_release():
    """The gate refuses to open short premium into a release. Holding one
    through it is the identical exposure — guarding the front door while leaving
    the back one open would be no guard at all."""
    d = evaluate_position(
        credit_view(current_price=-1.00), CFG, LATER, macro_window=FakeMacroWindow()
    )
    assert d.should_close and d.barrier is Barrier.MACRO_RISK
    assert "Nonfarm" in d.reason and d.label is not None


def test_long_convexity_is_held_through_a_release():
    """A debit position cannot be assigned and profits from a large move; the
    release is not a reason to abandon it."""
    d = evaluate_position(
        debit_view(current_price=2.10), CFG, LATER, macro_window=FakeMacroWindow()
    )
    assert d.action is Action.HOLD


def test_inactive_window_changes_nothing():
    d = evaluate_position(
        credit_view(current_price=-1.00),
        CFG,
        LATER,
        macro_window=FakeMacroWindow(active=False),
    )
    assert d.action is Action.HOLD


def test_deadline_still_outranks_the_macro_exit():
    d = evaluate_position(
        credit_view(current_price=-1.00),
        CFG,
        LATER,
        deadline=OPENED,
        macro_window=FakeMacroWindow(),
    )
    assert d.barrier is Barrier.DEADLINE


def test_macro_exit_outranks_stop_and_target():
    """Being flat for a known event is an obligation, not an opportunity."""
    winner = evaluate_position(
        credit_view(current_price=-0.10), CFG, LATER, macro_window=FakeMacroWindow()
    )
    loser = evaluate_position(
        credit_view(current_price=-3.70), CFG, LATER, macro_window=FakeMacroWindow()
    )
    assert winner.barrier is Barrier.MACRO_RISK
    assert loser.barrier is Barrier.MACRO_RISK


# --- thesis completion ----------------------------------------------------


def thesis_view(direction="up", predicted=5.0, entry_spot=100.0, spot=100.0, **kw):
    base = {
        "position_id": "pos-t",
        "kind": StructureKind.CALL_DEBIT_SPREAD,
        "qty": 1,
        "entry_price": 2.00,
        "current_price": 2.10,
        "max_loss_per_spread": 200.0,
        "opened_at": OPENED,
        "horizon_hours": 24.0,
        "hours_to_expiry": 168.0,
        "thesis_direction": direction,
        "thesis_move_pct": predicted,
        "entry_spot": entry_spot,
        "current_spot": spot,
    }
    base.update(kw)
    return PositionView(**base)


def test_thesis_completes_when_the_move_arrives():
    """We predicted +5% and the stock did +5%. The forecast move has happened —
    there is nothing further to wait for."""
    d = evaluate_position(thesis_view(spot=105.0), CFG, LATER)
    assert d.should_close and d.barrier is Barrier.THESIS_COMPLETE
    assert "the forecast move has happened" in d.reason


def test_partial_move_does_not_complete_the_thesis():
    assert evaluate_position(thesis_view(spot=102.0), CFG, LATER).action is Action.HOLD


def test_move_in_the_wrong_direction_does_not_complete_it():
    """A stock that travelled the predicted distance the wrong way has not
    completed the thesis, it has falsified it — that is the stop's job."""
    d = evaluate_position(thesis_view(direction="up", spot=95.0), CFG, LATER)
    assert d.barrier is not Barrier.THESIS_COMPLETE


def test_downward_thesis_completes_on_a_fall():
    d = evaluate_position(thesis_view(direction="down", spot=95.0), CFG, LATER)
    assert d.barrier is Barrier.THESIS_COMPLETE


def test_vol_only_thesis_completes_on_either_side():
    """A vol_only view predicts magnitude without a sign."""
    for spot in (105.0, 95.0):
        d = evaluate_position(thesis_view(direction="vol_only", spot=spot), CFG, LATER)
        assert d.barrier is Barrier.THESIS_COMPLETE


def test_missing_spot_cannot_evaluate_completion():
    """Unavailable spot means "cannot evaluate", never "no movement"."""
    assert evaluate_position(thesis_view(spot=0.0), CFG, LATER).action is Action.HOLD


def test_obligations_still_outrank_completion():
    complete = thesis_view(spot=105.0)
    assert evaluate_position(complete, CFG, LATER, deadline=OPENED).barrier is Barrier.DEADLINE
