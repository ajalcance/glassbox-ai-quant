"""Corporate action blackout tests.

A short call into an ex-dividend date is the ordinary way a defined-risk spread
becomes an unexpected stock position. These pin down when we refuse to trade.
"""

from datetime import date

from glassbox.data.corporate import CorporateEvent, blackout

TODAY = date(2026, 9, 1)
HORIZON = date(2026, 9, 15)


def event(kind="dividend", day=date(2026, 9, 5), symbol="XOM"):
    return CorporateEvent(symbol=symbol, kind=kind, effective=day)


def test_no_events_does_not_block():
    r = blackout([], HORIZON, is_credit=True, today=TODAY)
    assert not r.blocked and "no corporate action" in r.detail


def test_dividend_blocks_a_credit_structure():
    """The short leg can be assigned early to capture the dividend."""
    r = blackout([event("dividend")], HORIZON, is_credit=True, today=TODAY)
    assert r.blocked
    assert "assignment risk" in r.detail and "dividend" in r.detail


def test_dividend_does_not_block_a_long_structure():
    """A purely long position cannot be assigned, so a dividend is not a reason
    to refuse it."""
    r = blackout([event("dividend")], HORIZON, is_credit=False, today=TODAY)
    assert not r.blocked


def test_split_blocks_both_sides():
    """A split changes the deliverable, so max-loss arithmetic stops describing
    the position regardless of which way it is facing."""
    for is_credit in (True, False):
        r = blackout([event("split")], HORIZON, is_credit=is_credit, today=TODAY)
        assert r.blocked and "contract terms change" in r.detail


def test_merger_and_spinoff_block_both_sides():
    for kind in ("merger", "spinoff"):
        for is_credit in (True, False):
            assert blackout([event(kind)], HORIZON, is_credit, TODAY).blocked


def test_event_beyond_the_horizon_does_not_block():
    """We are out of the position before it happens."""
    r = blackout([event("split", date(2026, 10, 20))], HORIZON, True, TODAY)
    assert not r.blocked


def test_past_event_does_not_block():
    """Alpaca's announcements endpoint returns actions whose ex-date has already
    passed when filtered on the declaration date; a stale one must not block."""
    r = blackout([event("dividend", date(2026, 8, 17))], HORIZON, True, TODAY)
    assert not r.blocked


def test_event_on_the_boundary_blocks():
    r = blackout([event("dividend", HORIZON)], HORIZON, True, TODAY)
    assert r.blocked


def test_blocking_events_are_reported():
    events = [event("dividend"), event("split", date(2026, 9, 8))]
    r = blackout(events, HORIZON, is_credit=True, today=TODAY)
    assert r.blocked and len(r.events) == 2
