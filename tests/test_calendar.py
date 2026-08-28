"""Market calendar tests. `minutes_to_close` assumed a 16:00 close; on an early
close that is wrong by three hours, disabling the end-of-session guard exactly
when liquidity is worst."""

from datetime import date, datetime

from glassbox.clock import MARKET_TZ
from glassbox.data.calendar import Session


def session(close_hour=16, close_minute=0):
    day = date(2026, 11, 27)
    return Session(
        day=day,
        open_at=datetime(2026, 11, 27, 9, 30, tzinfo=MARKET_TZ),
        close_at=datetime(2026, 11, 27, close_hour, close_minute, tzinfo=MARKET_TZ),
        is_early_close=close_hour < 16,
    )


def at(hour, minute=0):
    return datetime(2026, 11, 27, hour, minute, tzinfo=MARKET_TZ)


def test_regular_session_math():
    s = session()
    assert s.minutes_since_open(at(10, 30)) == 60
    assert s.minutes_to_close(at(15, 30)) == 30


def test_early_close_shortens_the_session():
    """The day after Thanksgiving closes at 13:00. Assuming 16:00 would report
    three hours of trading time that does not exist."""
    early = session(close_hour=13)
    assert early.is_early_close
    assert early.minutes_to_close(at(12, 50)) == 10
    assert session().minutes_to_close(at(12, 50)) == 190


def test_after_close_clamps_to_zero():
    assert session().minutes_to_close(at(17, 0)) == 0
    assert session().minutes_since_open(at(9, 0)) == 0
