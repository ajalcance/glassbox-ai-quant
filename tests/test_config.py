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
