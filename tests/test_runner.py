"""Runner tests focused on resilience: a bad story, a dead socket, or a failing
reconcile must not end the session while positions are open."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from glassbox.audit import AuditLog
from glassbox.runner import DryRunRouter, build_universe
from glassbox.signal.filter import NewsItem


def test_dry_run_router_records_without_submitting(tmp_path):
    audit = AuditLog(tmp_path)
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

    audit = AuditLog(tmp_path)
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
        line for line in (tmp_path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").read_text().splitlines()
    ]
    assert any("pipeline_error" in k for k in kinds)


def test_duplicate_news_processed_once(tmp_path):
    """The socket and the poller both see the same story."""
    from glassbox.runner import Runner

    calls = []
    runner = FakeRunner(
        AuditLog(tmp_path),
        raiser=lambda item, state: calls.append(item) or SimpleNamespace(traded=False, reason="ok"),
    )
    item = NewsItem(
        id="n1", symbol="AAPL", headline="x", summary="", source="s", created_at=datetime.now(UTC)
    )
    Runner.handle_news(runner, item)
    Runner.handle_news(runner, item)
    assert len(calls) == 1
