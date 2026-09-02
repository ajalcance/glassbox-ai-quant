"""Scheduled macro releases — when not to trust our own signal.

Near a high-impact release every straddle in the universe carries embedded
event premium. The edge test compares a stock-specific expectation against that
inflated straddle, reads "overpriced", and would happily sell insurance right
before the insured event. So inside the window:

  * no new short-premium positions — that is the trade the distortion
    manufactures;
  * long-convexity entries are permitted but haircut, since the same premium
    makes them expensive;
  * after the release clears, the news it generates flows through the ordinary
    pipeline against post-event straddles.

Contest week uses a hand-verified table in config. Four dates checked by hand
beat any API for a four-day contest: zero dependencies, zero failure modes, and
the schedule is auditable in the repo. The interface takes a calendar provider
later without the callers changing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class MacroWindow:
    active: bool
    event_name: str = ""
    event_at: datetime | None = None
    minutes_until: float | None = None  # negative = event already passed
    detail: str = "no scheduled release nearby"

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "event": self.event_name,
            "minutes_until": self.minutes_until,
            "detail": self.detail,
        }


def _parse_events(cfg) -> list[tuple[datetime, str]]:
    out = []
    for event in cfg.macro.events:
        try:
            out.append((datetime.fromisoformat(event.at), event.name))
        except ValueError:
            continue  # a malformed entry must not take the pipeline down
    return sorted(out)


def next_session_open(now: datetime) -> datetime:
    """09:30 ET on the next weekday after `now`. Holidays are a post-contest
    concern; the contest week has none after Labor Day."""
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    local = now.astimezone(et)
    day = local.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 9, 30, tzinfo=et)


def release_before_next_open(cfg, now: datetime) -> MacroWindow:
    """A release landing between now and the next session's first tick.

    The blackout window guards the front door during the session and is
    blind at exactly the moment a carried position needs it: ADP printed
    2 Sep at 08:15 ET, its window closed at 08:45, and the manager's first
    tick came at 09:30 — a credit carried overnight sat straight through it.
    This is what the bell gate asks before letting a credit carry.
    """
    after = timedelta(minutes=cfg.macro.blackout_after_minutes)
    horizon = next_session_open(now) + after
    for at, name in _parse_events(cfg):
        if now < at <= horizon:
            hours = (at - now).total_seconds() / 3600
            return MacroWindow(
                True, name, at, hours * 60, f"{name} in {hours:.1f}h, before the next open"
            )
    return MacroWindow(False)


def current_window(cfg, now: datetime) -> MacroWindow:
    """Is `now` inside the blackout window of any configured release?"""
    before = timedelta(hours=cfg.macro.blackout_before_hours)
    after = timedelta(minutes=cfg.macro.blackout_after_minutes)

    for at, name in _parse_events(cfg):
        if at - before <= now <= at + after:
            minutes = (at - now).total_seconds() / 60
            when = f"in {minutes:.0f}m" if minutes >= 0 else f"{-minutes:.0f}m ago"
            return MacroWindow(
                True,
                name,
                at,
                minutes,
                f"{name} {when} — straddles carry event premium",
            )

    upcoming = [(at, name) for at, name in _parse_events(cfg) if at > now]
    if upcoming:
        at, name = upcoming[0]
        hours = (at - now).total_seconds() / 3600
        return MacroWindow(
            False,
            name,
            at,
            hours * 60,
            f"next release {name} in {hours:.1f}h",
        )
    return MacroWindow(False)
