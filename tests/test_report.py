"""Nightly report tests.

The report is evidence, so the tests focus on it being derived from the log
rather than narrated into existence — and on a quiet day reading as a quiet day
rather than a failure.
"""

from datetime import date

from glassbox.audit import AuditLog
from glassbox.clock import now_utc
from glassbox.report.generate import DayStats, collect, render_markdown


def seed(tmp_path, *entries):
    log = AuditLog(tmp_path, role="trader")
    for kind, payload in entries:
        log.append(kind, payload)
    return now_utc().date()


def test_empty_day_is_valid_not_an_error(tmp_path):
    stats = collect(tmp_path, date(2026, 9, 1))
    assert stats.news_seen == 0 and stats.orders_submitted == 0


def test_funnel_counts_each_stage(tmp_path):
    day = seed(
        tmp_path,
        (
            "signal_dropped",
            {
                "stage": "filter",
                "symbol": "A",
                "news_id": "1",
                "headline": "h",
                "reason": "boilerplate",
            },
        ),
        ("analyst_view", {"signal_id": "B-1", "symbol": "B", "headline": "h"}),
        ("edge_test", {"signal_id": "B-1", "verdict": "long_convexity", "ratio": 1.6}),
        ("gate", {"signal_id": "B-1", "approved": True, "checks": []}),
        ("order_submit", {"position_id": "pos-B-1", "structure": "s", "qty": 1}),
    )
    s = collect(tmp_path, day)
    assert s.news_seen == 2 and s.reached_analyst == 1
    assert s.tradable_edge == 1 and s.gate_approved == 1 and s.orders_submitted == 1
    assert dict(s.funnel)["orders placed"] == 1


def test_no_edge_does_not_count_as_tradable(tmp_path):
    day = seed(
        tmp_path,
        ("analyst_view", {"signal_id": "A-1", "symbol": "A", "headline": "h"}),
        ("edge_test", {"signal_id": "A-1", "verdict": "no_edge", "ratio": 0.98}),
    )
    assert collect(tmp_path, day).tradable_edge == 0


def test_vetoes_and_drops_are_attributed(tmp_path):
    day = seed(
        tmp_path,
        (
            "gate",
            {
                "signal_id": "1",
                "approved": False,
                "checks": [
                    {"name": "liquidity", "passed": False, "detail": ""},
                    {"name": "portfolio_heat", "passed": True, "detail": ""},
                ],
            },
        ),
        (
            "signal_dropped",
            {"stage": "filter", "symbol": "X", "news_id": "n", "headline": "h", "reason": "r"},
        ),
    )
    s = collect(tmp_path, day)
    assert s.vetoes_by_check == {"liquidity": 1}
    assert s.drops_by_stage == {"filter": 1}


def test_closed_positions_aggregate_pnl_and_barriers(tmp_path):
    day = seed(
        tmp_path,
        (
            "manage",
            {
                "action": "close",
                "barrier": "profit",
                "unrealized_pnl": 60.0,
                "label": 1,
                "position_id": "p1",
            },
        ),
        (
            "manage",
            {
                "action": "close",
                "barrier": "stop",
                "unrealized_pnl": -120.0,
                "label": 0,
                "position_id": "p2",
            },
        ),
        ("manage", {"action": "hold", "barrier": "none", "position_id": "p3"}),
    )
    s = collect(tmp_path, day)
    assert s.positions_closed == 2 and s.wins == 1 and s.losses == 1
    assert s.realized_pnl == -60.0
    assert s.exits_by_barrier == {"profit": 1, "stop": 1}
    assert s.win_rate == 0.5


def test_halts_and_errors_are_surfaced(tmp_path):
    day = seed(
        tmp_path,
        ("halt", {"reason": "reconciliation mismatch"}),
        ("pipeline_error", {"error": "ValueError: x"}),
        ("stream_error", {"error": "ConnectionError"}),
    )
    s = collect(tmp_path, day)
    assert "reconciliation mismatch" in s.halts
    assert s.errors == {"pipeline_error": 1, "stream_error": 1}


# --- rendering ------------------------------------------------------------

LEARNING = {"meta_labeler": {"trained": False, "n_samples": 4, "min_samples": 30}, "bandit": []}


def test_quiet_day_renders_without_a_narrative(tmp_path):
    md = render_markdown(DayStats(day="2026-09-01"), LEARNING, "", None)
    assert "# GlassBox — 2026-09-01" in md
    assert "news seen" in md


def test_untrained_meta_labeler_is_stated_plainly(tmp_path):
    md = render_markdown(DayStats(day="2026-09-01"), LEARNING, "", None)
    assert "abstaining (4/30" in md
    assert "analyst's own confidence" in md


def test_small_samples_are_labelled_as_meaningless():
    stats = DayStats(
        day="2026-09-01",
        positions_closed=2,
        wins=1,
        losses=1,
        realized_pnl=-60.0,
        exits_by_barrier={"profit": 1, "stop": 1},
    )
    md = render_markdown(stats, LEARNING, "", None)
    assert "Too few outcomes" in md, "two trades must not be presented as evidence"


def test_cli_disagreement_is_flagged_for_investigation():
    md = render_markdown(
        DayStats(day="2026-09-01"),
        LEARNING,
        "",
        {"equity": 100000, "position_count": 2, "agrees": False},
    )
    assert "NO — investigate" in md


def test_narrative_failure_does_not_block_the_report(tmp_path, monkeypatch):
    """The numbers are the report; the prose is a convenience."""
    from glassbox.config import load_config
    from glassbox.report import generate
    from glassbox.store import Store

    monkeypatch.setattr(
        generate,
        "narrative",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    store = Store(tmp_path / "t.db")
    path = generate.write_report(cfg, store, llm=object(), day=date(2026, 9, 1))
    store.close()
    assert path.exists() and "Narrative unavailable" in path.read_text()
