from glassbox.config import load_config


def test_default_config_loads_and_matches_plan():
    load_config.cache_clear()
    cfg = load_config()
    assert cfg.account.paper is True
    assert cfg.account.starting_equity == 100000
    assert cfg.risk.r_per_trade_pct == 0.5
    assert cfg.risk.portfolio_heat_pct == 6.0
    assert cfg.risk.max_drawdown_halt_pct == 6.0
    assert cfg.risk.min_meta_label_p == 0.55
    assert cfg.signal.edge_ratio_debit > 1 > cfg.signal.edge_ratio_credit
    assert cfg.llm.provider == "fireworks"


def test_market_date_uses_eastern_not_local():
    """PHT is a calendar day ahead of ET during the session — using the local
    date to pick expiries would silently choose the wrong contracts."""
    from datetime import datetime

    from glassbox.clock import MARKET_TZ, market_date, now_market, parse_expiry

    assert now_market().tzinfo is MARKET_TZ
    assert market_date() == now_market().date()
    assert parse_expiry("2026-09-18") == parse_expiry(
        datetime(2026, 9, 18, 16, 0, tzinfo=MARKET_TZ)
    )


def test_supervisor_falls_back_to_main_credentials(monkeypatch):
    """A paper account has one key pair; the supervisor must still start."""
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PKTEST")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    monkeypatch.delenv("SUPERVISOR_ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("SUPERVISOR_ALPACA_API_SECRET_KEY", raising=False)

    from glassbox.data.alpaca_client import supervisor_trading_client, trading_client

    sup, trader = supervisor_trading_client(), trading_client()
    assert sup is not trader, "supervisor must hold its own client instance"
