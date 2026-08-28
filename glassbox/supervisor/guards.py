"""Account guards — the last line of defence.

These run in the supervisor process, which polls the broker independently of
the trader. If the trader wedges, loops, or dies mid-position, the guards still
fire: they read equity from Alpaca directly and never consult trader state.

Guard actions are deliberately blunt. There is no clever recovery — flatten and
halt, then require a human to look. An automated system that tries to trade its
way out of a drawdown is how accounts end.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

KILL_SWITCH_FILE = "KILL"
PEAK_EQUITY_KEY = "peak_equity"
SESSION_START_EQUITY_KEY = "session_start_equity"
LOSS_STREAK_KEY = "loss_streak"


class GuardAction(StrEnum):
    CONTINUE = "continue"
    HALT_SESSION = "halt_session"  # flatten, stop for today
    HALT_HARD = "halt_hard"  # flatten, stop entirely, manual reset


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    action: GuardAction
    reason: str
    daily_pnl_pct: float = 0.0
    drawdown_pct: float = 0.0

    @property
    def should_flatten(self) -> bool:
        return self.action is not GuardAction.CONTINUE


def evaluate_guards(
    equity: float,
    session_start_equity: float,
    peak_equity: float,
    cfg,
    kill_switch: bool = False,
    heartbeat_age_seconds: float = 0.0,
    heartbeat_timeout: float = 90.0,
) -> GuardVerdict:
    """Pure evaluation of the account guards. No I/O."""
    if kill_switch:
        return GuardVerdict(GuardAction.HALT_HARD, "kill switch engaged by operator")

    if session_start_equity <= 0 or peak_equity <= 0:
        return GuardVerdict(GuardAction.CONTINUE, "no baseline yet")

    daily_pnl_pct = 100 * (equity - session_start_equity) / session_start_equity
    drawdown_pct = 100 * (equity - peak_equity) / peak_equity

    # Hardest condition first: a max-drawdown breach outranks a daily breach.
    if drawdown_pct <= -cfg.risk.max_drawdown_halt_pct:
        return GuardVerdict(
            GuardAction.HALT_HARD,
            f"drawdown {drawdown_pct:.2f}% breached -{cfg.risk.max_drawdown_halt_pct}% "
            f"(peak ${peak_equity:,.0f} -> ${equity:,.0f}); manual reset required",
            daily_pnl_pct,
            drawdown_pct,
        )

    if daily_pnl_pct <= -cfg.risk.daily_loss_halt_pct:
        return GuardVerdict(
            GuardAction.HALT_SESSION,
            f"daily loss {daily_pnl_pct:.2f}% breached -{cfg.risk.daily_loss_halt_pct}%",
            daily_pnl_pct,
            drawdown_pct,
        )

    # A trader that stopped heartbeating may be wedged mid-position. We cannot
    # know what it intended, so we take the risk off rather than guess.
    if heartbeat_age_seconds > heartbeat_timeout:
        return GuardVerdict(
            GuardAction.HALT_SESSION,
            f"trader heartbeat stale ({heartbeat_age_seconds:.0f}s > {heartbeat_timeout:.0f}s)",
            daily_pnl_pct,
            drawdown_pct,
        )

    return GuardVerdict(GuardAction.CONTINUE, "within limits", daily_pnl_pct, drawdown_pct)


def update_peak_equity(store, equity: float, broker_peak: float | None = None) -> float:
    """Peak equity ratchets up only — drawdown is measured from the high-water
    mark, not from wherever the session happened to start.

    `broker_peak` comes from the account's own portfolio history and is folded
    in because local state is not durable enough to hold this number. Wiping the
    database — a fresh deploy, a `docker compose down -v` — would reset the peak
    to current equity, making drawdown read 0% and silently disabling the guard
    until a new peak formed. The broker remembers what we might not.
    """
    peak = float(store.get_state(PEAK_EQUITY_KEY, "0") or 0)
    candidates = [peak, equity]
    if broker_peak:
        candidates.append(broker_peak)
    highest = max(candidates)
    if highest > peak:
        store.set_state(PEAK_EQUITY_KEY, str(highest))
    return highest


def broker_peak_equity(trading_client, period: str = "1M") -> float | None:
    """Highest equity the broker has recorded, or None if unavailable.

    None rather than a default: an unavailable history must not be read as
    "no drawdown", which is what a zero would mean downstream.
    """
    from alpaca.trading.requests import GetPortfolioHistoryRequest

    try:
        history = trading_client.get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe="1D")
        )
    except Exception:  # noqa: BLE001 -- unavailable history is not fatal; the
        # locally tracked peak still applies.
        return None
    values = [float(v) for v in (getattr(history, "equity", None) or []) if v]
    return max(values) if values else None
