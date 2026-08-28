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
        "pattern_day_trader": False,
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


def test_pdt_below_25k_warns_but_does_not_block():
    """The system may legitimately run smaller — but it must be known."""
    result = run(FakeClient(account(equity="10000", pattern_day_trader=True)))
    check = names(result)["pattern_day_trader"]
    assert not check.passed and not check.fatal
    assert result.ok, "a PDT warning must not prevent startup"
    assert "PDT rules" in check.detail


def test_unreachable_broker_is_reported_not_raised():
    result = run(FakeClient(raises=ConnectionError("503")))
    assert not result.ok
    assert "account_reachable" in names(result)


def test_all_problems_reported_at_once():
    """One thing at a time is a slow way to fix a broken account."""
    result = run(FakeClient(account(status="ONBOARDING", options_trading_level=1)))
    failed = {c.name for c in result.failures}
    assert {"account_active", "options_level"} <= failed
