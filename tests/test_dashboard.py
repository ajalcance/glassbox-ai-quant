"""Dashboard tests.

The dashboard is read-only by design — the demo URL is public, so the page can
observe state and nothing else. These tests assert that property alongside the
correlation logic that turns a flat audit log into decision rows.
"""

import json

import pytest
from fastapi.testclient import TestClient

from glassbox.audit import AuditLog
from glassbox.dashboard.app import app
from glassbox.dashboard.audit_reader import (
    audit_chain_status,
    build_decisions,
    read_records,
    summarise_vetoes,
)


@pytest.fixture
def client():
    return TestClient(app)


# --- the read-only guarantee ---------------------------------------------


def test_no_mutating_routes_exist():
    """A public page that can place or cancel orders is a liability. Controls
    live in the CLI on the host; this surface can only read."""
    methods = {m for route in app.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}, f"dashboard exposes {methods - {'GET', 'HEAD'}}"


def test_docs_are_disabled():
    """No interactive API explorer on a public endpoint."""
    assert app.docs_url is None and app.redoc_url is None


# --- endpoints ------------------------------------------------------------


def test_index_serves_the_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "GlassBox" in r.text
    assert "app.alpaca.markets" in r.text, "must link out rather than rebuild the broker view"


def test_state_endpoint_shape(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert {"risk", "learning", "health"} <= set(body)
    assert "heat_cap" in body["risk"]
    assert "meta_labeler" in body["learning"]
    assert "audit_chain" in body["health"]


def test_vetoes_and_decisions_endpoints(client):
    assert client.get("/api/vetoes").status_code == 200
    assert "decisions" in client.get("/api/decisions").json()


# --- correlating a flat log into decisions --------------------------------


def _records(tmp_path, *entries):
    log = AuditLog(tmp_path, role="trader")
    for kind, payload in entries:
        log.append(kind, payload)
    return read_records(tmp_path)


def test_decision_assembles_from_separate_records(tmp_path):
    records = _records(
        tmp_path,
        (
            "analyst_view",
            {"signal_id": "AAPL-1", "symbol": "AAPL", "headline": "Apple beats", "direction": "up"},
        ),
        ("edge_test", {"signal_id": "AAPL-1", "verdict": "long_convexity", "ratio": 1.6}),
        ("ml", {"signal_id": "AAPL-1", "meta_label_p": 0.7, "regime": "normal_vol"}),
        ("gate", {"signal_id": "AAPL-1", "approved": True, "checks": []}),
        ("order_submit", {"position_id": "pos-AAPL-1", "structure": "k", "qty": 2}),
    )
    d = build_decisions(records)[0]
    assert d.symbol == "AAPL" and d.outcome == "traded"
    assert d.edge["ratio"] == 1.6 and d.ml["meta_label_p"] == 0.7


def test_vetoed_decision_reports_the_failing_checks(tmp_path):
    records = _records(
        tmp_path,
        ("analyst_view", {"signal_id": "SPY-2", "symbol": "SPY", "headline": "x"}),
        (
            "gate",
            {
                "signal_id": "SPY-2",
                "approved": False,
                "checks": [
                    {"name": "liquidity", "passed": False, "detail": "spread 22% > 5%"},
                    {"name": "portfolio_heat", "passed": True, "detail": "ok"},
                ],
            },
        ),
    )
    d = build_decisions(records)[0]
    assert d.outcome == "vetoed"
    assert "liquidity" in d.reason and "22%" in d.reason


def test_dropped_signal_without_signal_id_still_appears(tmp_path):
    """Filter-stage drops happen before a signal id exists; they must not vanish."""
    records = _records(
        tmp_path,
        (
            "signal_dropped",
            {
                "stage": "filter",
                "symbol": "TSLA",
                "news_id": "n9",
                "headline": "Market Update",
                "reason": "boilerplate",
            },
        ),
    )
    d = build_decisions(records)[0]
    assert d.outcome == "dropped" and d.symbol == "TSLA"
    assert "boilerplate" in d.reason


def test_decisions_are_newest_first(tmp_path):
    records = _records(
        tmp_path,
        ("analyst_view", {"signal_id": "A-1", "symbol": "A", "headline": "first"}),
        ("analyst_view", {"signal_id": "B-2", "symbol": "B", "headline": "second"}),
    )
    assert [d.symbol for d in build_decisions(records)] == ["B", "A"]


# --- veto summary ---------------------------------------------------------


def test_veto_summary_counts_by_check_and_stage(tmp_path):
    records = _records(
        tmp_path,
        (
            "gate",
            {
                "signal_id": "1",
                "approved": False,
                "checks": [{"name": "liquidity", "passed": False, "detail": ""}],
            },
        ),
        (
            "gate",
            {
                "signal_id": "2",
                "approved": False,
                "checks": [
                    {"name": "liquidity", "passed": False, "detail": ""},
                    {"name": "portfolio_heat", "passed": False, "detail": ""},
                ],
            },
        ),
        ("gate", {"signal_id": "3", "approved": True, "checks": []}),
        (
            "signal_dropped",
            {"stage": "filter", "symbol": "X", "news_id": "n", "headline": "h", "reason": "r"},
        ),
    )
    s = summarise_vetoes(records).as_dict()
    assert dict(s["by_check"])["liquidity"] == 2
    assert s["total_traded"] == 1
    assert s["total_seen"] == 4


def test_malformed_log_lines_are_skipped_not_fatal(tmp_path):
    """The dashboard must never be the reason you cannot see the system."""
    (tmp_path / "2026-09-01.jsonl").write_text('{"ok":1}\nnot json\n{"kind":"gate"}\n')
    assert len(read_records(tmp_path)) == 2


def test_missing_audit_directory_is_not_an_error(tmp_path):
    assert read_records(tmp_path / "nope") == []


def test_chain_status_reports_verification(tmp_path):
    from glassbox.clock import now_utc

    log = AuditLog(tmp_path, role="trader")
    for i in range(3):
        log.append("gate", {"i": i})
    status = audit_chain_status(tmp_path)
    assert status["verified"] and status["records"] == 3

    path = tmp_path / f"{now_utc():%Y-%m-%d}-trader.jsonl"
    lines = path.read_bytes().splitlines()
    tampered = json.loads(lines[1])
    tampered["i"] = 99
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(b"\n".join(lines) + b"\n")
    assert not audit_chain_status(tmp_path)["verified"]
