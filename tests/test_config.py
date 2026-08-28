from glassbox.config import load_config


def test_default_config_loads_and_matches_plan():
    load_config.cache_clear()
    cfg = load_config()
    assert cfg.account.paper is True
    assert cfg.account.starting_equity == 100000
    # R is a sizing preference and may be tuned; these are the relationships
    # that must hold whatever it is set to. Per-trade risk must stay below the
    # per-position cap, which must stay below the heat cap, or the backstops
    # stop being backstops.
    assert 0 < cfg.risk.r_per_trade_pct <= cfg.risk.max_loss_per_position_pct
    assert cfg.risk.max_loss_per_position_pct < cfg.risk.portfolio_heat_pct
    assert cfg.risk.daily_loss_halt_pct < cfg.risk.max_drawdown_halt_pct
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


def test_dashboard_port_avoids_common_dev_ports():
    load_config.cache_clear()
    port = load_config().dashboard.port
    assert port not in {3000, 4200, 5000, 5173, 8000, 8080, 8888, 9000}


def test_report_token_budget_accounts_for_reasoning():
    """The report model reasons before answering and that reasoning is charged
    against max_tokens. A small budget truncates the answer to nothing while the
    request still returns successfully."""
    load_config.cache_clear()
    cfg = load_config()
    assert cfg.llm.report_max_tokens >= 2000
    assert cfg.llm.report_max_tokens > cfg.llm.analyst_max_tokens
