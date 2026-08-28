"""Alpaca CLI integration tests.

The CLI is an independent verification source, so the behaviour that matters is
what happens when it is unavailable: an unrun check must never be reported as
a passing one.
"""

from glassbox import alpaca_cli


def test_missing_cli_reports_unavailable_not_agreement(monkeypatch):
    """A verification that passes when it did not run is worse than none."""
    monkeypatch.setattr(alpaca_cli, "is_available", lambda: False)
    check = alpaca_cli.cross_check([])
    assert check.available is False
    assert check.agrees is False, "an unavailable check must never read as agreement"
    assert "not installed" in check.detail


def test_cli_failure_reports_unavailable(monkeypatch):
    monkeypatch.setattr(alpaca_cli, "is_available", lambda: True)
    monkeypatch.setattr(
        alpaca_cli,
        "account",
        lambda: (_ for _ in ()).throw(alpaca_cli.CliUnavailableError("TLS timeout")),
    )
    check = alpaca_cli.cross_check([])
    assert not check.available and not check.agrees
    assert "TLS timeout" in check.detail


def test_agreement_when_both_sources_match(monkeypatch):
    monkeypatch.setattr(alpaca_cli, "is_available", lambda: True)
    monkeypatch.setattr(alpaca_cli, "account", lambda: {"equity": "100000"})
    monkeypatch.setattr(alpaca_cli, "positions", lambda: [{"symbol": "A"}])
    check = alpaca_cli.cross_check([{"symbol": "A"}])
    assert check.agrees and check.equity == 100000.0


def test_disagreement_is_detected(monkeypatch):
    """The whole point: the CLI shares no code with the trader, so a divergence
    here means one of the two views of the account is wrong."""
    monkeypatch.setattr(alpaca_cli, "is_available", lambda: True)
    monkeypatch.setattr(alpaca_cli, "account", lambda: {"equity": "100000"})
    monkeypatch.setattr(alpaca_cli, "positions", list)
    check = alpaca_cli.cross_check([{"symbol": "A"}, {"symbol": "B"}])
    assert check.available and not check.agrees
    assert "CLI sees 0" in check.detail and "SDK sees 2" in check.detail


def test_env_mapping_bridges_our_names_to_the_cli(monkeypatch):
    """Our variables are named for the SDK; the CLI expects different names.
    Mapping them here avoids duplicating secrets in .env."""
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PKTEST")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    env = alpaca_cli._env()
    assert env["ALPACA_API_KEY"] == "PKTEST"
    assert env["ALPACA_SECRET_KEY"] == "secret"
    assert env["ALPACA_PAPER"] == "true"


def test_no_write_commands_are_exposed():
    """Every CLI call is read-only. No order ever goes through this module."""
    public = {n for n in dir(alpaca_cli) if not n.startswith("_")}
    forbidden = {"place_order", "submit", "cancel", "close_position", "buy", "sell"}
    assert not (public & forbidden)
