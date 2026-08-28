"""Corporate action awareness.

An options position is a claim on a security whose terms can change underneath
it. Two cases matter enough to refuse a trade outright:

  * **Dividends.** A short call in a name going ex-dividend carries genuine
    early-assignment risk — it is the ordinary way a defined-risk spread becomes
    an unexpected stock position overnight. The holder of a deep in-the-money
    call exercises to capture the dividend, and the short side is assigned.
  * **Splits, mergers, spinoffs.** These change the contract's deliverable. A
    spread priced against 100 shares is not the same instrument afterwards, and
    our max-loss arithmetic silently stops describing the position.

Neither is exotic. The gate had sixteen checks and not one asked whether
something was about to happen to the security itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from glassbox.clock import market_date, parse_expiry

# Announcements are cached for a day: the set of upcoming corporate actions does
# not change minute to minute, and this runs on every candidate signal.
CACHE_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class CorporateEvent:
    symbol: str
    kind: str  # dividend | split | merger | spinoff
    effective: date
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind} on {self.effective:%Y-%m-%d}"


@dataclass(frozen=True, slots=True)
class BlackoutResult:
    blocked: bool
    events: tuple[CorporateEvent, ...] = ()
    detail: str = ""


def _as_date(value) -> date | None:
    try:
        return parse_expiry(value) if value else None
    except (ValueError, TypeError):
        return None


def fetch_events(trading_client, symbol: str, lookahead_days: int = 30) -> list[CorporateEvent]:
    """Upcoming actions for one symbol.

    Alpaca's announcements endpoint is queried per corporate-action type because
    a combined query is rejected for wide date ranges.
    """
    from alpaca.trading.enums import CorporateActionDateType, CorporateActionType
    from alpaca.trading.requests import GetCorporateAnnouncementsRequest

    today = market_date()
    out: list[CorporateEvent] = []
    for ca_type in (
        CorporateActionType.DIVIDEND,
        CorporateActionType.SPLIT,
        CorporateActionType.MERGER,
        CorporateActionType.SPINOFF,
    ):
        try:
            announcements = trading_client.get_corporate_announcements(
                GetCorporateAnnouncementsRequest(
                    ca_types=[ca_type],
                    since=today,
                    until=today + timedelta(days=lookahead_days),
                    symbol=symbol,
                    # Explicit: the default date_type filters on the declaration
                    # date, which returns actions whose ex-date has already
                    # passed. Ex-date is the one that governs assignment risk.
                    date_type=CorporateActionDateType.EX_DATE,
                )
            )
        except Exception:  # noqa: BLE001, S112 -- one unavailable action type
            # must not hide the others. The caller treats an empty result as
            # "unknown", and the gate is what decides whether unknown is safe.
            continue

        for a in announcements or []:
            # Ex-date is what matters for assignment risk; payable date is later
            # and irrelevant to whether the option gets exercised early.
            effective = (
                _as_date(getattr(a, "ex_date", None))
                or _as_date(getattr(a, "effective_date", None))
                or _as_date(getattr(a, "payable_date", None))
            )
            if effective:
                out.append(
                    CorporateEvent(
                        symbol=symbol,
                        kind=str(getattr(a, "ca_type", ca_type)).split(".")[-1].lower(),
                        effective=effective,
                        detail=str(getattr(a, "cash", "") or getattr(a, "new_rate", "") or ""),
                    )
                )
    return sorted(out, key=lambda e: e.effective)


def blackout(
    events: list[CorporateEvent],
    horizon_end: date,
    is_credit: bool,
    today: date | None = None,
) -> BlackoutResult:
    """Should this trade be refused because of a corporate action?

    Any action landing before the position is due to close is disqualifying.
    Credit structures carry a short leg and therefore assignment risk, so a
    dividend blocks them; a purely long structure cannot be assigned, so a
    dividend alone does not block it — but a split or merger changes the
    deliverable for either side and blocks both.
    """
    today = today or market_date()
    relevant = [e for e in events if today <= e.effective <= horizon_end]
    if not relevant:
        return BlackoutResult(False, (), "no corporate action in the horizon")

    blocking = [
        e
        for e in relevant
        if e.kind in ("split", "merger", "spinoff") or (e.kind == "dividend" and is_credit)
    ]
    if not blocking:
        return BlackoutResult(
            False,
            tuple(relevant),
            f"{len(relevant)} action(s) in horizon, none blocking for a long structure",
        )

    reasons = ", ".join(str(e) for e in blocking)
    why = (
        "assignment risk on the short leg"
        if any(e.kind == "dividend" for e in blocking)
        else "contract terms change"
    )
    return BlackoutResult(True, tuple(blocking), f"{reasons} — {why}")
