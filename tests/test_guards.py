import pytest

from glassbox.config import load_config
from glassbox.supervisor.guards import GuardAction, evaluate_guards, update_peak_equity

CFG = load_config()
START = 100_000.0


def guard(equity, peak=START, **kw):
    return evaluate_guards(equity, START, peak, CFG, **kw)


def test_within_limits_continues():
    assert guard(100_500).action is GuardAction.CONTINUE


def test_daily_loss_halts_session():
    v = guard(97_900)  # -2.1%
    assert v.action is GuardAction.HALT_SESSION and v.should_flatten
    assert "daily loss" in v.reason


def test_drawdown_halts_hard_and_outranks_daily():
    v = guard(93_000, peak=100_000)  # -7% from peak and -7% on day
    assert v.action is GuardAction.HALT_HARD
    assert "drawdown" in v.reason and "manual reset" in v.reason


def test_drawdown_measured_from_peak_not_session_start():
    """Up 10% then back to flat is a 9% drawdown, even though the day is flat."""
    v = evaluate_guards(100_000, START, peak_equity=110_000, cfg=CFG)
    assert v.action is GuardAction.HALT_HARD
    assert v.drawdown_pct == pytest.approx(-9.09, abs=0.01)


def test_kill_switch_outranks_everything():
    v = guard(101_000, kill_switch=True)
    assert v.action is GuardAction.HALT_HARD and "kill switch" in v.reason


def test_stale_heartbeat_flattens():
    v = guard(100_000, heartbeat_age_seconds=200, heartbeat_timeout=90)
    assert v.action is GuardAction.HALT_SESSION and "heartbeat" in v.reason


def test_fresh_heartbeat_fine():
    assert guard(100_000, heartbeat_age_seconds=10).action is GuardAction.CONTINUE


def test_no_baseline_is_safe():
    assert evaluate_guards(100_000, 0, 0, CFG).action is GuardAction.CONTINUE


def test_peak_equity_ratchets_up_only(store):
    assert update_peak_equity(store, 100_000) == 100_000
    assert update_peak_equity(store, 105_000) == 105_000
    assert update_peak_equity(store, 99_000) == 105_000, "peak must not fall"


def test_hard_halt_is_reported_once_not_looped(store, audit, tmp_path, capsys):
    """Under a restart policy, exiting on a hard halt produces a crash loop that
    re-flattens every few seconds and reads as a broken system. The supervisor
    must stay alive and keep watching instead."""
    from glassbox.supervisor import run as supervisor_run

    class FakeAccount:
        equity = "100000"

    class FakeClient:
        def __init__(self):
            self.flattens = 0

        def get_account(self):
            return FakeAccount()

        def cancel_orders(self):
            self.flattens += 1
            return []

        def close_all_positions(self, cancel_orders=True):
            return []

    (tmp_path / "KILL").touch()
    client = FakeClient()
    for _ in range(3):
        action = supervisor_run.tick(store, audit, client, CFG, tmp_path, dry_run=False)
        assert action is GuardAction.HALT_HARD
    assert client.flattens == 3, "each tick flattens; the loop must not exit"


# --- drawdown reference ---------------------------------------------------


def test_broker_peak_survives_a_wiped_database(store):
    """The max-drawdown guard measured from a locally held peak. Wiping the
    database reset it to current equity, making drawdown read 0% and silently
    disabling the guard until a new peak formed."""
    from glassbox.supervisor.guards import update_peak_equity

    # Fresh store: nothing known locally, but the broker remembers a higher mark.
    peak = update_peak_equity(store, equity=94_000, broker_peak=110_000)
    assert peak == 110_000

    verdict = evaluate_guards(94_000, 100_000, peak, CFG)
    assert verdict.action is GuardAction.HALT_HARD, (
        "a 14% drawdown from the true peak must halt, even though local state had never seen it"
    )


def test_local_peak_wins_when_broker_history_is_unavailable(store):
    from glassbox.supervisor.guards import update_peak_equity

    update_peak_equity(store, 108_000)
    assert update_peak_equity(store, 100_000, broker_peak=None) == 108_000


def test_peak_never_decreases_from_a_lower_broker_value(store):
    from glassbox.supervisor.guards import update_peak_equity

    update_peak_equity(store, 120_000)
    assert update_peak_equity(store, 100_000, broker_peak=105_000) == 120_000
