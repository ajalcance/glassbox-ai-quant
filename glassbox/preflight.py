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


def run(trading_client, market_data=None) -> Preflight:
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

    return Preflight(tuple(checks))


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
