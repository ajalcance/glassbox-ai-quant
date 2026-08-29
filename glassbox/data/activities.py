"""Account activity — assignment and expiration events.

alpaca-py exposes no method for the activities endpoint, so this calls the REST
API directly. That is worth the small amount of plumbing because the events it
carries are ones position reconciliation cannot see.

Reconciliation compares option symbols on both sides. When a short leg is
assigned, the option ceases to exist and becomes a stock position — both sides
agree that the option is gone, and nothing flags that we are now holding
equity we never chose to hold, with none of the defined-risk properties the
gate approved. `OPASN` is the only place that shows up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

PAPER_BASE = "https://paper-api.alpaca.markets"
ASSIGNMENT = "OPASN"
EXPIRATION = "OPEXP"


@dataclass(frozen=True, slots=True)
class Activity:
    activity_type: str
    symbol: str
    date: str
    qty: str = ""
    description: str = ""

    @property
    def is_assignment(self) -> bool:
        return self.activity_type == ASSIGNMENT

    def __str__(self) -> str:
        label = "assigned" if self.is_assignment else "expired"
        return f"{self.symbol} {label} ({self.qty})".strip()


def _timeout() -> httpx.Timeout:
    """Fully bounded timeout from config, matching every other broker call.

    All four phases are set explicitly: a 2-tuple leaves write and pool as
    None, and "unbounded" is the exact property the broker-timeout rule exists
    to eliminate — a half-open socket must never block a caller forever.
    """
    from glassbox.config import load_config

    cfg = load_config().execution
    return httpx.Timeout(
        connect=cfg.broker_connect_timeout_seconds,
        read=cfg.broker_read_timeout_seconds,
        write=cfg.broker_connect_timeout_seconds,
        pool=cfg.broker_connect_timeout_seconds,
    )


def fetch(api_key: str, secret_key: str, activity_type: str, on: date | None = None) -> list:
    """One activity type for one day. Raises on transport failure; the caller
    decides whether an unavailable check is safe."""
    params = {"date": on.isoformat()} if on else {}
    response = httpx.get(
        f"{PAPER_BASE}/v2/account/activities/{activity_type}",
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
        params=params,
        # Same bounded-call rule as every Alpaca SDK client: this is the one
        # broker request that does not route through _with_default_timeout.
        timeout=_timeout(),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []
    return [
        Activity(
            activity_type=a.get("activity_type", activity_type),
            symbol=a.get("symbol", ""),
            date=a.get("date", ""),
            qty=str(a.get("qty", "")),
            description=a.get("description", ""),
        )
        for a in payload
    ]


def option_events(api_key: str, secret_key: str, on: date | None = None) -> list[Activity]:
    """Assignments and expirations for a day, newest first."""
    out: list[Activity] = []
    for kind in (ASSIGNMENT, EXPIRATION):
        out.extend(fetch(api_key, secret_key, kind, on))
    return out
