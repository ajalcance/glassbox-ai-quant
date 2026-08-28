"""Market-time helpers.

The operator runs in Philippine time (UTC+8); the market runs in US Eastern.
Using the local date to pick expiries or bound a trading day is a real bug —
PHT is a calendar day ahead of ET for most of the trading session. Every
date-sensitive decision goes through here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_market() -> datetime:
    """Current time in US Eastern — the only clock that matters for sessions."""
    return datetime.now(MARKET_TZ)


def market_date() -> date:
    """Today's date *in the market's timezone*, not the operator's."""
    return now_market().date()


def parse_expiry(value) -> date:
    """Normalise an Alpaca expiration_date field to a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
