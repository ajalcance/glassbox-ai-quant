"""Runner tests focused on resilience: a bad story, a dead socket, or a failing
reconcile must not end the session while positions are open."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from glassbox.audit import AuditLog
from glassbox.runner import DryRunRouter, build_universe
from glassbox.signal.filter import NewsItem


def test_dry_run_router_records_without_submitting(tmp_path):
    audit = AuditLog(tmp_path, role="trader")
    router = DryRunRouter(audit)
    from glassbox.structures import Leg, LegSide, Right, Structure, StructureKind

    real = Structure(
        StructureKind.BULL_PUT_SPREAD,
        "SPY",
        (
            Leg("SPY260918P00440000", Right.PUT, 440, date(2026, 9, 18), LegSide.SHORT),
            Leg("SPY260918P00435000", Right.PUT, 435, date(2026, 9, 18), LegSide.LONG),
        ),
    )
    order = router.submit_structure(real, 2, -1.20, "gbx-o-1", "pos-1")
    assert order.id.startswith("dry-")
    assert len(router.submitted) == 1 and router.submitted[0]["qty"] == 2


def test_universe_is_static_and_liquid():
    """A universe that changes underneath the agent is one more thing that can
    break unattended overnight."""
    u = build_universe(None)
    assert len(u) >= 40
    assert {"SPY", "QQQ", "AAPL", "NVDA"} <= u
    assert all(s.isupper() and 1 <= len(s) <= 5 for s in u)


class FakeRunner:
    """Exercises Runner.handle_news error handling without any network."""

    def __init__(self, audit, raiser):
        self.audit = audit
        self._seen_news = set()
        self.trader = SimpleNamespace(process_news=raiser)

    def market_state(self):
        return None

    handle_news = None


def test_pipeline_error_is_logged_and_swallowed(tmp_path, capsys):
    """One malformed story must never end a session holding positions."""
    from glassbox.runner import Runner

    audit = AuditLog(tmp_path, role="trader")
    runner = FakeRunner(audit, raiser=lambda item, state: 1 / 0)
    Runner.handle_news(
        runner,
        NewsItem(
            id="n1",
            symbol="AAPL",
            headline="x",
            summary="",
            source="s",
            created_at=datetime.now(UTC),
        ),
    )
    kinds = [
        line
        for line in (tmp_path / f"{datetime.now(UTC):%Y-%m-%d}-trader.jsonl")
        .read_text()
        .splitlines()
    ]
    assert any("pipeline_error" in k for k in kinds)


def test_store_is_usable_from_the_stream_thread(tmp_path):
    """The news socket delivers on its own thread but shares the Runner's Store.
    A connection bound to the main thread would make every socket story fail at
    its first store access — and, once marked seen, be skipped by the poller."""
    import threading

    from glassbox.store import Store

    store = Store(tmp_path / "s.db")
    store.set_state("main", "1")
    errors = []

    def stream_thread():
        try:
            store.set_state("stream", "2")
            assert store.get_state("main") == "1"
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=stream_thread)
    t.start()
    t.join()
    assert not errors, errors
    assert store.get_state("stream") == "2"
    store.close()


def test_duplicate_news_processed_once(tmp_path):
    """The socket and the poller both see the same story."""
    from glassbox.runner import Runner

    calls = []
    runner = FakeRunner(
        AuditLog(tmp_path, role="trader"),
        raiser=lambda item, state: calls.append(item) or SimpleNamespace(traded=False, reason="ok"),
    )
    item = NewsItem(
        id="n1", symbol="AAPL", headline="x", summary="", source="s", created_at=datetime.now(UTC)
    )
    Runner.handle_news(runner, item)
    Runner.handle_news(runner, item)
    assert len(calls) == 1


class LoopRunner:
    """Exercises Runner.guarded_step and Runner.tick's heartbeat gate without
    a broker. `tick_raises` models Alpaca returning 500 for a while."""

    def __init__(self, audit, max_tick_failures=3):
        self.audit = audit
        self.max_tick_failures = max_tick_failures
        self._consecutive_failures = 0
        self.tick_raises = False
        self.ticks = 0
        self.polls = 0

    def tick(self):
        self.ticks += 1
        if self.tick_raises:
            raise RuntimeError('{"message":"Internal Server Error"}')

    def poll_news(self):
        self.polls += 1


def test_a_failing_tick_does_not_end_the_process(tmp_path, capsys):
    """11 Sep: an unguarded APIError from /v2/clock killed the trader 79 times
    in 48 minutes while two live positions sat unmanaged."""
    from glassbox.runner import Runner

    runner = LoopRunner(AuditLog(tmp_path, role="trader"))
    runner.tick_raises = True
    for _ in range(5):
        Runner.guarded_step(runner)  # must not raise
    assert runner.ticks == 5, "the loop keeps running through a broker outage"
    assert runner._consecutive_failures == 5


def test_transient_failure_is_absorbed_and_the_counter_resets(tmp_path):
    """A blip must not escalate. Only a persistent fault should."""
    from glassbox.runner import Runner

    runner = LoopRunner(AuditLog(tmp_path, role="trader"))
    runner.tick_raises = True
    Runner.guarded_step(runner)
    Runner.guarded_step(runner)
    assert runner._consecutive_failures == 2
    runner.tick_raises = False
    Runner.guarded_step(runner)
    assert runner._consecutive_failures == 0, "recovery clears the escalation path"
    assert runner.polls == 1, "poll_news only runs on a tick that got that far"


def test_heartbeat_is_withheld_once_ticks_stop_completing(tmp_path):
    """The counter has to actually reach the supervisor. A trader stamping
    "I am fine" while every tick throws blinds the only thing left that can
    act — worse than crashing, because then nothing escalates at all."""
    from glassbox.reconcile import HALT_KEY
    from glassbox.runner import Runner
    from glassbox.store import Store

    beats = []
    runner = LoopRunner(AuditLog(tmp_path, role="trader"), max_tick_failures=3)
    runner.store = Store(tmp_path / "s.db")
    # Halting makes tick() return right after the heartbeat gate, so this
    # exercises the gate itself rather than the broker calls behind it.
    runner.store.set_state(HALT_KEY, "under test")
    runner.trader = SimpleNamespace(heartbeat=lambda: beats.append(1))

    for failures, expected in ((0, 1), (1, 2), (2, 3), (3, 3), (4, 3), (99, 3)):
        runner._consecutive_failures = failures
        Runner.tick(runner)
        assert len(beats) == expected, f"after {failures} consecutive failures"
    runner.store.close()


def _audit_records(audit):
    import json

    return [
        json.loads(line)
        for path in sorted(audit.dir.glob("*.jsonl"))
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def test_failures_are_audited_with_the_escalation_flag(tmp_path):
    from glassbox.runner import Runner

    audit = AuditLog(tmp_path, role="trader")
    runner = LoopRunner(audit, max_tick_failures=2)
    runner.tick_raises = True
    Runner.guarded_step(runner)
    Runner.guarded_step(runner)
    errors = [r for r in _audit_records(audit) if r["kind"] == "tick_error"]
    assert len(errors) == 2
    assert errors[0]["heartbeat_suppressed"] is False
    assert errors[1]["heartbeat_suppressed"] is True, "the supervisor must get its signal"


def test_recovery_is_recorded(tmp_path):
    """A silent recovery makes the incident impossible to reconstruct later."""
    from glassbox.runner import Runner

    audit = AuditLog(tmp_path, role="trader")
    runner = LoopRunner(audit)
    runner.tick_raises = True
    Runner.guarded_step(runner)
    runner.tick_raises = False
    Runner.guarded_step(runner)
    recovered = [r for r in _audit_records(audit) if r["kind"] == "tick_recovered"]
    assert recovered and recovered[0]["after"] == 1
