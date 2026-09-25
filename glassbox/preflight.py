"""Startup preconditions.

The trader depends on facts about the account that it cannot influence: options
enabled at a level that permits multi-leg spreads, the account not blocked, a
reachable market calendar. Discovering any of these is false at 03:00 with
positions open is a much worse way to learn it was never true.

Each check returns a verdict rather than raising, so the report shows everything
that is wrong at once instead of one thing at a time.
"""

from __future__ import annotations

from dataclasses import dataclass

# Level 3 permits multi-leg spreads. Anything lower cannot express a single
# structure this system knows how to build.
REQUIRED_OPTIONS_LEVEL = 3


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str
    fatal: bool = True

    def __str__(self) -> str:
        mark = "ok  " if self.passed else ("FAIL" if self.fatal else "warn")
        return f"  {mark}  {self.name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Preflight:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(c.passed or not c.fatal for c in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed and c.fatal)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail, "fatal": c.fatal}
                for c in self.checks
            ],
        }


def _account_checks(account) -> list[Check]:
    status = str(getattr(account, "status", "")).split(".")[-1]
    checks = [
        Check("account_active", status == "ACTIVE", f"status {status}"),
        Check(
            "not_blocked",
            not (
                getattr(account, "trading_blocked", False)
                or getattr(account, "account_blocked", False)
            ),
            "trading permitted"
            if not getattr(account, "trading_blocked", False)
            else "TRADING BLOCKED",
        ),
    ]

    level = getattr(account, "options_trading_level", None)
    if level is None:
        checks.append(Check("options_level", False, "account did not report an options level"))
    else:
        level = int(level)
        checks.append(
            Check(
                "options_level",
                level >= REQUIRED_OPTIONS_LEVEL,
                f"level {level}"
                + (
                    " — multi-leg spreads permitted"
                    if level >= REQUIRED_OPTIONS_LEVEL
                    else f" — need {REQUIRED_OPTIONS_LEVEL} for multi-leg spreads"
                ),
            )
        )

    equity = float(getattr(account, "equity", 0) or 0)
    # PDT applies below $25k and would silently reject intraday round trips.
    # Not fatal: the system may legitimately run smaller, but it must be known.
    checks.append(
        Check(
            "pattern_day_trader",
            equity >= 25_000 or not getattr(account, "pattern_day_trader", False),
            f"equity ${equity:,.0f}"
            + (
                "" if equity >= 25_000 else " — below $25k, PDT rules restrict intraday round trips"
            ),
            fatal=False,
        )
    )
    return checks


def run(trading_client, market_data=None, cfg=None) -> Preflight:
    """Assert the preconditions. Never raises; returns what it found."""
    checks: list[Check] = []

    try:
        checks.extend(_account_checks(trading_client.get_account()))
    except Exception as e:  # noqa: BLE001 -- an unreachable broker is itself the
        # finding, and is reported rather than thrown.
        checks.append(Check("account_reachable", False, f"{type(e).__name__}: {e}"))
        return Preflight(tuple(checks))

    try:
        config = trading_client.get_account_configurations()
        suspended = bool(getattr(config, "suspend_trade", False))
        checks.append(
            Check(
                "trade_not_suspended",
                not suspended,
                "suspend_trade is set" if suspended else "trading enabled",
            )
        )
    except Exception as e:  # noqa: BLE001
        checks.append(Check("account_config", False, f"{type(e).__name__}: {e}", fatal=False))

    if market_data is not None and hasattr(market_data, "session"):
        session = market_data.session()
        if session is None:
            checks.append(Check("market_calendar", False, "calendar unavailable", fatal=False))
        else:
            checks.append(
                Check(
                    "market_calendar",
                    True,
                    f"{session.open_at:%H:%M}–{session.close_at:%H:%M} ET"
                    + (" (EARLY CLOSE)" if session.is_early_close else ""),
                )
            )

    checks.append(_macro_calendar_check(cfg))
    try:
        checks.append(_account_size_check(cfg, float(trading_client.get_account().equity)))
    except Exception as e:  # noqa: BLE001 -- reported, never thrown
        checks.append(Check("account_size", False, f"{type(e).__name__}: {e}", fatal=False))

    return Preflight(tuple(checks))


def _account_size_check(cfg, equity: float, tolerance: float = 2.0) -> Check:
    """Is the broker account roughly the size this config was built for?

    Every limit is a fraction of LIVE equity, so a config tuned for one account
    size runs without complaint on any other — and means something different
    there. On 25 Sep the system moved to a $10,000 configuration (r_per_trade
    1.5%, no abstain haircut) while the running paper account still held
    ~$98,600. Deploying that config onto that account would have made every
    position about 3x larger than the day before, silently. The percentages
    were right; the account was wrong.

    Not fatal: the percentages still bound risk on any account. But a mismatch
    beyond `tolerance`x in either direction is a deployment mistake until
    someone says otherwise, and it should be the first line anyone reads.
    """
    if cfg is None:
        return Check("account_size", True, "not checked", fatal=False)
    target = float(cfg.account.starting_equity)
    if target <= 0 or equity <= 0:
        return Check("account_size", True, "not checked", fatal=False)
    ratio = equity / target
    if ratio > tolerance or ratio < 1 / tolerance:
        return Check(
            "account_size",
            False,
            f"MISMATCH — config is sized for ${target:,.0f}, account holds ${equity:,.0f} "
            f"({ratio:.1f}x). Every limit scales with equity, so positions are "
            f"{ratio:.1f}x what this config was tuned for.",
            fatal=False,
        )
    return Check("account_size", True, f"${equity:,.0f} vs ${target:,.0f} target", fatal=False)


def _macro_calendar_check(cfg) -> Check:
    """Does the macro calendar still describe the future?

    The calendar is hand-maintained — a deliberate choice, since four verified
    dates beat an API nobody has exercised. The cost of that choice is that it
    goes stale silently: the blackout simply stops matching anything, the
    system reports no macro risk, and it looks exactly like a quiet week. It
    held only contest-week dates for five sessions after the contest ended and
    nothing said so.

    Not fatal — a stale calendar must not stop the trader, only be visible.
    """
    from glassbox.clock import now_utc

    if cfg is None:
        return Check("macro_calendar", True, "not checked", fatal=False)
    try:
        from glassbox.macro import _parse_events

        events = _parse_events(cfg)
    except Exception as e:  # noqa: BLE001 -- a malformed calendar is the finding
        return Check("macro_calendar", False, f"unreadable: {type(e).__name__}: {e}", fatal=False)

    now = now_utc()
    upcoming = [(at, name) for at, name in events if at > now]
    if not upcoming:
        latest = f", newest is {max(at for at, _ in events):%Y-%m-%d}" if events else ""
        return Check(
            "macro_calendar",
            False,
            f"STALE — {len(events)} event(s), none in the future{latest}. "
            "The macro blackout and the bell gate's premarket lookahead are inert",
            fatal=False,
        )
    at, name = upcoming[0]
    days = (at - now).days
    return Check(
        "macro_calendar", True,
        f"{len(upcoming)} upcoming, next {name} in {days}d ({at:%Y-%m-%d %H:%M %Z})",
    )


def main() -> int:

    from glassbox.config import load_config
    from glassbox.data.alpaca_client import trading_client

    load_config()
    result = run(trading_client())
    print("GlassBox preflight")
    for check in result.checks:
        print(check)
    print("PREFLIGHT PASSED" if result.ok else "PREFLIGHT FAILED")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
