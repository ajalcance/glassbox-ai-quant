"""Market session times from the exchange calendar.

`minutes_to_close` was computed against an assumed 16:00 close. Early closes
exist — the day after Thanksgiving, Christmas Eve, Independence Day eve — and on
those days the assumption is wrong by three hours, which silently disables the
end-of-session guard exactly when liquidity is worst.

No half-day falls inside this contest, so nothing here changes the outcome. It
is a correctness gap, and cheap enough that leaving it open would be a choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from glassbox.clock import MARKET_TZ, market_date


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    open_at: datetime
    close_at: datetime
    is_early_close: bool

    def minutes_since_open(self, now: datetime) -> int:
        return max(0, int((now - self.open_at).total_seconds() // 60))

    def minutes_to_close(self, now: datetime) -> int:
        return max(0, int((self.close_at - now).total_seconds() // 60))


def _combine(day: date, t) -> datetime:
    return datetime(day.year, day.month, day.day, t.hour, t.minute, tzinfo=MARKET_TZ)


def fetch_session(trading_client, day: date | None = None) -> Session | None:
    """The exchange's own times for a day, or None when unavailable.

    None rather than a default: a caller that cannot learn the session times
    should not proceed on an assumption about them.
    """
    from alpaca.trading.requests import GetCalendarRequest

    day = day or market_date()
    try:
        days = trading_client.get_calendar(
            GetCalendarRequest(start=day, end=day + timedelta(days=1))
        )
    except Exception:  # noqa: BLE001 -- calendar unavailable is not fatal; the
        # caller decides what to do with an unknown session.
        return None

    for entry in days or []:
        if entry.date == day:
            open_at, close_at = _combine(day, entry.open), _combine(day, entry.close)
            return Session(
                day=day,
                open_at=open_at,
                close_at=close_at,
                # A regular session closes at 16:00 ET; anything earlier is a
                # half day and shortens every end-of-session calculation.
                is_early_close=close_at.hour < 16,
            )
    return None
