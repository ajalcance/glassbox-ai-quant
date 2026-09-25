"""Preflight tests. Discovering options are not enabled at 03:00 with positions
open is a much worse way to learn it than asserting it at boot."""

from types import SimpleNamespace

from glassbox.preflight import run


def account(**kw):
    base = {
        "status": "ACTIVE",
        "trading_blocked": False,
        "account_blocked": False,
        "options_trading_level": 3,
        "equity": "100000",
        "options_buying_power": "100000",
    }
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    def __init__(self, acct=None, raises=None):
        self._acct = acct or account()
        self._raises = raises

    def get_account(self):
        if self._raises:
            raise self._raises
        return self._acct

    def get_account_configurations(self):
        return SimpleNamespace(suspend_trade=False)


def names(result):
    return {c.name: c for c in result.checks}


def test_healthy_account_passes():
    assert run(FakeClient()).ok


def test_options_level_below_three_is_fatal():
    """Level 2 cannot express a single structure this system builds."""
    result = run(FakeClient(account(options_trading_level=2)))
    assert not result.ok
    check = names(result)["options_level"]
    assert not check.passed and "need 3" in check.detail


def test_missing_options_level_is_fatal():
    result = run(FakeClient(account(options_trading_level=None)))
    assert not result.ok and not names(result)["options_level"].passed


def test_blocked_trading_is_fatal():
    result = run(FakeClient(account(trading_blocked=True)))
    assert not result.ok and "BLOCKED" in names(result)["not_blocked"].detail


def test_inactive_account_is_fatal():
    result = run(FakeClient(account(status="ONBOARDING")))
    assert not result.ok


def test_no_pattern_day_trader_check_remains():
    """FINRA retired PDT effective 4 June 2026. A check citing it described a
    rule that no longer existed, on every start of a $10,000 account."""
    from glassbox.config import load_config

    result = run(FakeClient(account(equity="10000")), cfg=load_config())
    assert "pattern_day_trader" not in names(result)


def test_insufficient_options_buying_power_warns_but_does_not_block():
    """Options are cash-collateralised: a spread's collateral is its max loss.
    Too little buying power for one full-size position must be visible."""
    from glassbox.config import load_config

    cfg = load_config()
    result = run(FakeClient(account(equity="10000", options_buying_power="40")), cfg=cfg)
    check = names(result)["options_buying_power"]
    assert not check.passed and not check.fatal
    assert result.ok

    ok = run(FakeClient(account(equity="10000", options_buying_power="10000")), cfg=cfg)
    assert names(ok)["options_buying_power"].passed


def test_unreachable_broker_is_reported_not_raised():
    result = run(FakeClient(raises=ConnectionError("503")))
    assert not result.ok
    assert "account_reachable" in names(result)


def test_all_problems_reported_at_once():
    """One thing at a time is a slow way to fix a broken account."""
    result = run(FakeClient(account(status="ONBOARDING", options_trading_level=1)))
    failed = {c.name for c in result.failures}
    assert {"account_active", "options_level"} <= failed


def test_account_size_mismatch_is_loud_but_not_fatal():
    """25 Sep: a $10,000 config nearly went onto a ~$98,600 account. Every limit
    scales with live equity, so it would have run — at ~3x the intended size."""
    from glassbox.config import load_config
    from glassbox.preflight import _account_size_check

    cfg = load_config()
    target = cfg.account.starting_equity
    wrong = _account_size_check(cfg, target * 9.86)
    assert not wrong.passed and not wrong.fatal
    assert "MISMATCH" in wrong.detail
    assert _account_size_check(cfg, target * 1.2).passed
    assert not _account_size_check(cfg, target * 0.3).passed, "too small is a mismatch too"
