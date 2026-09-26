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


class _HaltFakeAccount:
    equity = "100000"


class _HaltFakeClient:
    """A broker whose book can be told to refill, to test re-flattening."""

    def __init__(self, positions=()):
        self.flattens = 0
        self._positions = list(positions)

    def get_account(self):
        return _HaltFakeAccount()

    def cancel_orders(self):
        self.flattens += 1
        return []

    def close_all_positions(self, cancel_orders=True):
        self._positions = []
        return []

    def get_all_positions(self):
        return list(self._positions)


def _engage_kill_switch(tmp_path):
    from glassbox.supervisor.guards import KILL_SWITCH_FILE

    kill = tmp_path / KILL_SWITCH_FILE
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.touch()


def test_hard_halt_is_reported_once_not_looped(store, audit, tmp_path, capsys, monkeypatch):
    """Under a restart policy, exiting on a hard halt produces a crash loop that
    re-flattens every few seconds and reads as a broken system. The supervisor
    must stay alive and keep watching instead.

    Staying alive is not the same as re-acting. An unchanged breach is flattened
    once; the later ticks keep watching and keep reporting the halt. On 11 Sep
    the unchanged version fired eight times in two minutes against an already
    empty book.
    """
    from glassbox.supervisor import run as supervisor_run

    monkeypatch.setattr(supervisor_run.time, "sleep", lambda s: None)
    _engage_kill_switch(tmp_path)
    client = _HaltFakeClient()
    for _ in range(3):
        action = supervisor_run.tick(store, audit, client, CFG, tmp_path, dry_run=False)
        assert action is GuardAction.HALT_HARD, "the supervisor must keep watching, not exit"
    assert client.flattens == 1, "an unchanged breach is acted on once, not every tick"


def test_standing_halt_reflattens_when_inventory_reappears(store, audit, tmp_path, monkeypatch):
    """Skipping the repeat flatten must not become "never flatten again".

    The halt's promise is an empty book, not a written-down flag. If anything
    is on the book under a standing halt, it goes — however it got there.
    """
    from glassbox.supervisor import run as supervisor_run

    class Pos:
        symbol = "AAPL260918C00230000"

    monkeypatch.setattr(supervisor_run.time, "sleep", lambda s: None)
    _engage_kill_switch(tmp_path)
    client = _HaltFakeClient()

    supervisor_run.tick(store, audit, client, CFG, tmp_path, dry_run=False)
    assert client.flattens == 1

    supervisor_run.tick(store, audit, client, CFG, tmp_path, dry_run=False)
    assert client.flattens == 1, "empty book under a standing halt needs no action"

    client._positions = [Pos()]
    supervisor_run.tick(store, audit, client, CFG, tmp_path, dry_run=False)
    assert client.flattens == 2, "inventory under a standing halt must be flattened again"


def test_a_different_guard_is_a_new_breach(store, audit, tmp_path, monkeypatch):
    """A worse breach while already halted is a new breach, not an echo.

    Identity is the GUARD, not its wording: a standing heartbeat halt followed
    by the kill switch must act again, because a different guard fired.
    """
    from glassbox.reconcile import HALT_KEY
    from glassbox.supervisor import run as supervisor_run

    monkeypatch.setattr(supervisor_run.time, "sleep", lambda s: None)
    store.set_state(HALT_KEY, "trader heartbeat stale (120s > 90s)")
    store.set_state(supervisor_run.HALT_GUARD_KEY, "heartbeat")
    _engage_kill_switch(tmp_path)
    client = _HaltFakeClient()

    supervisor_run.tick(store, audit, client, CFG, tmp_path, dry_run=False)
    assert client.flattens == 1, "the kill switch is a different guard and must act"
    assert store.get_state(supervisor_run.HALT_GUARD_KEY) == "kill_switch"


def _stale_heartbeat(store, seconds):
    from datetime import timedelta

    from glassbox.clock import now_utc
    from glassbox.supervisor.run import HEARTBEAT_KEY

    store.set_state(HEARTBEAT_KEY, (now_utc() - timedelta(seconds=seconds)).isoformat())


def test_a_stale_heartbeat_is_acted_on_once_although_its_reason_keeps_changing(
    store, audit, tmp_path, monkeypatch
):
    """The case the re-fire fix was written for, with the REAL reason.

    The heartbeat reason carries a live count — "(660s > 90s)" then
    "(678s > 90s)" — so comparing reason text never matched and the supervisor
    re-audited and re-flattened on every tick of an outage: 8 times in 2
    minutes on 11 Sep, twice through an Alpaca timeout on 26 Sep. The first
    fix passed its test only because that test used the kill switch, whose
    reason never changes.
    """
    from glassbox.supervisor import run as supervisor_run

    monkeypatch.setattr(supervisor_run.time, "sleep", lambda s: None)
    client = _HaltFakeClient()
    reasons = []
    for age in (660, 678, 696, 714):
        _stale_heartbeat(store, age)
        supervisor_run.tick(store, audit, client, CFG, tmp_path, dry_run=False)
        reasons.append(store.get_state("halt_reason"))

    assert len(set(reasons)) == 1, "the latched reason is the first one, not rewritten"
    assert client.flattens == 1, "one outage, one flatten — however the reason text changes"


def test_a_stale_marker_never_suppresses_a_new_breach(store, audit, tmp_path, monkeypatch):
    """The guard marker only counts while a halt is standing. If it outlived a
    cleared halt, a genuinely new breach of the same guard would look already
    handled and the flatten would be skipped."""
    from glassbox.supervisor import run as supervisor_run

    monkeypatch.setattr(supervisor_run.time, "sleep", lambda s: None)
    store.set_state(supervisor_run.HALT_GUARD_KEY, "heartbeat")  # leftover
    assert not store.get_state("halt_reason"), "no halt is standing"
    client = _HaltFakeClient()

    _stale_heartbeat(store, 300)
    supervisor_run.tick(store, audit, client, CFG, tmp_path, dry_run=False)
    assert client.flattens == 1, "a leftover marker must not swallow a real breach"


def test_every_halting_verdict_names_its_guard():
    from glassbox.supervisor.guards import GuardAction, evaluate_guards

    cases = {
        "kill_switch": {"equity": 100_000, "kill_switch": True},
        "drawdown": {"equity": 90_000},
        "daily_loss": {"equity": 97_000, "peak_equity": 97_500},
        "heartbeat": {"equity": 100_000, "heartbeat_age_seconds": 500},
    }
    for guard, kw in cases.items():
        args = {
            "equity": 100_000,
            "session_start_equity": 100_000,
            "peak_equity": 100_000,
            "cfg": CFG,
        }
        args.update(kw)
        v = evaluate_guards(**args)
        assert v.action is not GuardAction.CONTINUE, guard
        assert v.guard == guard, (guard, v.reason)
    ok = evaluate_guards(100_000, 100_000, 100_000, CFG)
    assert ok.action is GuardAction.CONTINUE and ok.guard == ""


def test_flatten_retries_until_spread_legs_are_gone(audit, monkeypatch):
    """Found live by drill-flat: close_all_positions closes a spread's legs as
    independent positions in arbitrary order, and selling the long leg while
    the short still exists would create a naked short — rejected. One pass
    closed the short leg and left the long on the book. Flatten must verify
    and retry until the broker is actually flat."""
    from types import SimpleNamespace

    from glassbox.supervisor import run as supervisor_run

    class SpreadClient:
        def __init__(self):
            # Pass 1: long-leg close is refused (short leg still open).
            # Pass 2: with the short gone, the long leg closes fine.
            self.book = [
                SimpleNamespace(symbol="SPY260831C00772000"),  # long — sticks once
                SimpleNamespace(symbol="SPY260831C00783000"),  # short — closes
            ]
            self.close_calls = 0

        def cancel_orders(self):
            return []

        def close_all_positions(self, cancel_orders=True):
            self.close_calls += 1
            closed = [p for p in self.book if self.close_calls > 1 or "783" in p.symbol]
            self.book = [p for p in self.book if p not in closed]
            return closed

        def get_all_positions(self):
            return list(self.book)

    monkeypatch.setattr(supervisor_run.time, "sleep", lambda s: None)
    client = SpreadClient()
    n = supervisor_run.flatten_all(client, audit)
    assert client.book == [], "flatten must not stop while legs remain"
    assert client.close_calls == 2 and n == 2


def test_flatten_incomplete_is_audited_not_silent(audit, monkeypatch):
    """If the book still is not flat after every retry, that must be a loud
    audit event — a guard that half-worked and said nothing is the worst case."""
    from types import SimpleNamespace

    from glassbox.supervisor import run as supervisor_run

    class StuckClient:
        def cancel_orders(self):
            return []

        def close_all_positions(self, cancel_orders=True):
            return []

        def get_all_positions(self):
            return [SimpleNamespace(symbol="SPY260831C00772000")]

    monkeypatch.setattr(supervisor_run.time, "sleep", lambda s: None)
    supervisor_run.flatten_all(StuckClient(), audit, attempts=2)
    files = sorted(audit.dir.glob("*.jsonl"))
    text = files[-1].read_text()
    assert "flatten_incomplete" in text and "SPY260831C00772000" in text


def test_daily_baseline_resets_on_a_new_market_day(store, monkeypatch):
    """The daily-loss guard is only daily if its baseline rolls over. Written
    once and never cleared, a Monday drawdown permanently consumes Tuesday's
    budget and a Monday gain masks a Tuesday loss — the -2% halt silently
    becomes 'loss since first boot' across a multi-day contest."""
    from datetime import date

    from glassbox.supervisor import guards

    monkeypatch.setattr(guards, "market_date", lambda: date(2026, 8, 31), raising=False)
    import glassbox.clock as clock_mod

    monkeypatch.setattr(clock_mod, "market_date", lambda: date(2026, 8, 31))
    assert guards.session_baseline(store, 100_000.0) == 100_000.0
    # Same day, equity moved: the baseline must NOT follow it.
    assert guards.session_baseline(store, 97_000.0) == 100_000.0

    monkeypatch.setattr(clock_mod, "market_date", lambda: date(2026, 9, 1))
    assert guards.session_baseline(store, 97_000.0) == 97_000.0, "new day, new baseline"
    verdict = evaluate_guards(
        equity=96_000.0, session_start_equity=97_000.0, peak_equity=100_000.0, cfg=CFG
    )
    assert verdict.daily_pnl_pct == pytest.approx(-1.03, abs=0.01), (
        "day 2 loss must be measured from day 2's open, not day 1's"
    )


def test_kill_switch_lives_on_the_shared_data_volume(tmp_path):
    """The compose stack mounts one `state` volume at /app/data in every
    container and nothing mounts /app itself. A sentinel outside data/ would be
    invisible inside docker — an emergency stop that silently does nothing."""
    from glassbox.supervisor.guards import KILL_SWITCH_FILE
    from glassbox.supervisor.run import kill_switch_engaged

    assert KILL_SWITCH_FILE.startswith("data/")
    assert not kill_switch_engaged(tmp_path)
    kill = tmp_path / KILL_SWITCH_FILE
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.touch()
    assert kill_switch_engaged(tmp_path)


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
