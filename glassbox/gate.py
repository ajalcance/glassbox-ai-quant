"""The risk gate — the single approval point between a decision and an order.

A pure function: context in, verdict out. No I/O, no clock reads, no broker
calls. That makes it exhaustively testable, trivially auditable, and impossible
to accidentally bypass by importing something else.

Checks run in a fixed order, cheapest and most categorical first. Every check
records its result — passes included — because the log of what we *allowed* is
as much evidence as the log of what we blocked.

Nothing here consults the LLM or any model. Models influence `qty` and which
structure is proposed; the gate decides whether that proposal is permissible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from glassbox.portfolio import Greeks, PortfolioState, correlated_exposure
from glassbox.structures import Structure, UndefinedRiskError, assert_defined_risk


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"{'PASS' if self.passed else 'VETO'} {self.name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class GateContext:
    # --- proposal ---------------------------------------------------------
    structure: Structure
    qty: int
    max_loss_per_spread: float
    meta_label_p: float
    # --- account ----------------------------------------------------------
    equity: float
    daily_pnl_pct: float
    drawdown_pct: float
    # --- session ----------------------------------------------------------
    market_open: bool
    minutes_since_open: int
    minutes_to_close: int
    hours_to_expiry: float
    # The analyst's stated horizon, used to check the session has room for it.
    horizon_hours: float = 0.0
    # --- system -----------------------------------------------------------
    halted: bool = False
    kill_switch: bool = False
    # --- portfolio --------------------------------------------------------
    portfolio: PortfolioState | None = None
    post_trade_greeks: Greeks | None = None
    correlations: dict[tuple[str, str], float] = field(default_factory=dict)
    # --- liquidity --------------------------------------------------------
    spread_pct_of_mid: float = 0.0
    open_interest: int = 0
    # --- rate limiting ----------------------------------------------------
    orders_last_minute: int = 0
    new_positions_today: int = 0
    duplicate_open: bool = False
    # --- corporate actions ------------------------------------------------
    # None means "not checked". The gate treats that as a pass and says so,
    # rather than silently implying the security was cleared.
    corporate_blackout: object | None = None
    # --- macro releases -----------------------------------------------------
    macro_window: object | None = None

    @property
    def total_max_loss(self) -> float:
        return self.max_loss_per_spread * self.qty


@dataclass(frozen=True, slots=True)
class GateDecision:
    approved: bool
    checks: tuple[CheckResult, ...]

    @property
    def vetoes(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def reason(self) -> str:
        return "; ".join(f"{c.name}: {c.detail}" for c in self.vetoes) or "all checks passed"

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


# --- individual checks ----------------------------------------------------
# Each returns CheckResult. Order matters: categorical blocks come first so a
# halted system produces one clear reason rather than a wall of noise.


def _check_kill_switch(ctx, cfg) -> CheckResult:
    engaged = ctx.kill_switch
    return CheckResult(
        "kill_switch", not engaged, "engaged — operator stop" if engaged else "clear"
    )


def _check_halted(ctx, cfg) -> CheckResult:
    return CheckResult(
        "system_halted",
        not ctx.halted,
        "halted (reconciliation or guard)" if ctx.halted else "running",
    )


def _check_market_window(ctx, cfg) -> CheckResult:
    if not ctx.market_open:
        return CheckResult("market_window", False, "market closed")
    if ctx.minutes_since_open < cfg.gate.skip_first_minutes:
        return CheckResult(
            "market_window",
            False,
            f"{ctx.minutes_since_open}m since open < "
            f"{cfg.gate.skip_first_minutes}m (opening auction)",
        )
    if ctx.minutes_to_close < cfg.gate.skip_last_minutes:
        return CheckResult(
            "market_window",
            False,
            f"{ctx.minutes_to_close}m to close < {cfg.gate.skip_last_minutes}m",
        )
    return CheckResult("market_window", True, f"{ctx.minutes_to_close}m to close")


def _check_session_room(ctx, cfg) -> CheckResult:
    """Does the session have room for this thesis to play out?

    An intraday thesis entered with less time left than it needs cannot resolve
    before the close, so we would be paying entry cost for an outcome that has
    no room to occur. A multi-day thesis is exempt: holding overnight is what it
    is for.
    """
    if ctx.horizon_hours <= 0:
        return CheckResult("session_room", True, "no horizon supplied")
    if ctx.horizon_hours > cfg.gate.intraday_horizon_hours:
        return CheckResult(
            "session_room",
            True,
            f"{ctx.horizon_hours:.0f}h thesis spans sessions by design",
        )

    needed = ctx.horizon_hours * 60 * cfg.gate.min_session_fraction
    if ctx.minutes_to_close < needed:
        return CheckResult(
            "session_room",
            False,
            f"{ctx.minutes_to_close}m to close < {needed:.0f}m needed for a "
            f"{ctx.horizon_hours:.0f}h thesis",
        )
    return CheckResult(
        "session_room",
        True,
        f"{ctx.minutes_to_close}m to close covers a {ctx.horizon_hours:.0f}h thesis",
    )


def _check_defined_risk(ctx, cfg) -> CheckResult:
    """The invariant that must never fail. Structural, not numeric."""
    try:
        assert_defined_risk(ctx.structure)
    except UndefinedRiskError as e:
        return CheckResult("defined_risk", False, str(e))
    return CheckResult("defined_risk", True, f"{len(ctx.structure.legs)} legs, all covered")


def _check_position_size(ctx, cfg) -> CheckResult:
    if ctx.qty < 1:
        return CheckResult("position_size", False, f"qty {ctx.qty}")
    cap = ctx.equity * (cfg.risk.max_loss_per_position_pct / 100)
    if ctx.total_max_loss > cap:
        return CheckResult(
            "position_size",
            False,
            f"${ctx.total_max_loss:.0f} > ${cap:.0f} cap "
            f"({cfg.risk.max_loss_per_position_pct}% equity)",
        )
    return CheckResult("position_size", True, f"${ctx.total_max_loss:.0f} of ${cap:.0f} cap")


def _check_portfolio_heat(ctx, cfg) -> CheckResult:
    current = ctx.portfolio.heat if ctx.portfolio else 0.0
    cap = ctx.equity * (cfg.risk.portfolio_heat_pct / 100)
    projected = current + ctx.total_max_loss
    if projected > cap:
        return CheckResult(
            "portfolio_heat",
            False,
            f"${projected:.0f} > ${cap:.0f} cap ({cfg.risk.portfolio_heat_pct}% equity)",
        )
    return CheckResult("portfolio_heat", True, f"${projected:.0f} of ${cap:.0f}")


def _check_greeks(ctx, cfg) -> CheckResult:
    g = ctx.post_trade_greeks
    if g is None:
        return CheckResult("greeks_bands", True, "no greeks supplied")
    band = cfg.risk.delta_dollars_band
    if abs(g.delta_dollars) > band:
        return CheckResult(
            "greeks_bands", False, f"net delta ${g.delta_dollars:,.0f} outside ±${band:,.0f}"
        )
    return CheckResult("greeks_bands", True, f"net delta ${g.delta_dollars:,.0f}")


def _check_concentration(ctx, cfg) -> CheckResult:
    if not ctx.portfolio:
        return CheckResult("concentration", True, "no open positions")
    underlying = ctx.structure.underlying
    existing = ctx.portfolio.positions_by_underlying.get(underlying, 0)
    if existing >= cfg.gate.max_positions_per_underlying:
        return CheckResult("concentration", False, f"{existing} positions already in {underlying}")
    per_name_cap = ctx.equity * (cfg.risk.max_loss_per_underlying_pct / 100)
    if ctx.total_max_loss > per_name_cap:
        return CheckResult(
            "concentration",
            False,
            f"${ctx.total_max_loss:.0f} in {underlying} > ${per_name_cap:.0f}",
        )
    return CheckResult("concentration", True, f"{existing} existing in {underlying}")


def _check_correlation(ctx, cfg) -> CheckResult:
    if not ctx.portfolio or not ctx.correlations:
        return CheckResult("correlation", True, "no correlation data")
    n = correlated_exposure(
        ctx.portfolio.positions_by_underlying,
        ctx.structure.underlying,
        ctx.correlations,
        threshold=cfg.gate.correlation_threshold,
    )
    if n >= cfg.gate.max_correlated_positions:
        return CheckResult(
            "correlation", False, f"{n} positions in names correlated to {ctx.structure.underlying}"
        )
    return CheckResult("correlation", True, f"{n} correlated positions")


def _check_liquidity(ctx, cfg) -> CheckResult:
    if ctx.spread_pct_of_mid > cfg.gate.max_spread_pct_of_mid:
        return CheckResult(
            "liquidity",
            False,
            f"spread {ctx.spread_pct_of_mid:.1f}% > {cfg.gate.max_spread_pct_of_mid}% of mid",
        )
    if ctx.open_interest < cfg.gate.min_open_interest:
        return CheckResult(
            "liquidity", False, f"OI {ctx.open_interest} < {cfg.gate.min_open_interest}"
        )
    return CheckResult(
        "liquidity", True, f"spread {ctx.spread_pct_of_mid:.1f}%, OI {ctx.open_interest}"
    )


def _check_time_to_expiry(ctx, cfg) -> CheckResult:
    if ctx.hours_to_expiry < cfg.manage.min_hours_to_expiry:
        return CheckResult(
            "time_to_expiry",
            False,
            f"{ctx.hours_to_expiry:.0f}h < {cfg.manage.min_hours_to_expiry}h "
            "(gamma risk into expiry)",
        )
    return CheckResult("time_to_expiry", True, f"{ctx.hours_to_expiry:.0f}h")


def _check_daily_loss(ctx, cfg) -> CheckResult:
    if ctx.daily_pnl_pct <= -cfg.risk.daily_loss_halt_pct:
        return CheckResult(
            "daily_loss", False, f"{ctx.daily_pnl_pct:.2f}% <= -{cfg.risk.daily_loss_halt_pct}%"
        )
    return CheckResult("daily_loss", True, f"{ctx.daily_pnl_pct:+.2f}% today")


def _check_drawdown(ctx, cfg) -> CheckResult:
    if ctx.drawdown_pct <= -cfg.risk.max_drawdown_halt_pct:
        return CheckResult(
            "max_drawdown", False, f"{ctx.drawdown_pct:.2f}% <= -{cfg.risk.max_drawdown_halt_pct}%"
        )
    return CheckResult("max_drawdown", True, f"{ctx.drawdown_pct:+.2f}% from peak")


def _check_rate_limits(ctx, cfg) -> CheckResult:
    """An algorithm repeats its mistakes until something stops it."""
    if ctx.orders_last_minute >= cfg.gate.max_orders_per_minute:
        return CheckResult(
            "rate_limit",
            False,
            f"{ctx.orders_last_minute} orders in last minute >= {cfg.gate.max_orders_per_minute}",
        )
    if ctx.new_positions_today >= cfg.gate.max_new_positions_per_day:
        return CheckResult(
            "rate_limit",
            False,
            f"{ctx.new_positions_today} new positions today >= "
            f"{cfg.gate.max_new_positions_per_day}",
        )
    return CheckResult(
        "rate_limit", True, f"{ctx.orders_last_minute}/min, {ctx.new_positions_today} today"
    )


def _check_macro_blackout(ctx, cfg) -> CheckResult:
    """No new short premium into a scheduled macro release.

    Near a release, every straddle carries embedded event premium. The edge
    test reads that as "overpriced" and proposes exactly the wrong trade:
    selling insurance minutes before the insured event. Long convexity is
    permitted — the same distortion only makes it expensive, and sizing already
    haircuts it — but the short-premium trade the distortion manufactures is
    refused outright.
    """
    window = ctx.macro_window
    if window is None:
        return CheckResult("macro_blackout", True, "not checked")
    if not getattr(window, "active", False):
        return CheckResult("macro_blackout", True, window.detail)
    if ctx.structure is not None and getattr(ctx.structure, "is_credit", False):
        return CheckResult("macro_blackout", False, window.detail)
    return CheckResult("macro_blackout", True, f"{window.detail}; long structure permitted")


def _check_corporate_action(ctx, cfg) -> CheckResult:
    """Refuse a position whose underlying is about to change under it.

    A short call into an ex-dividend date is the ordinary way a defined-risk
    spread becomes an unexpected stock position, and a split or merger changes
    the deliverable so our max-loss arithmetic stops describing the trade.
    """
    blackout = ctx.corporate_blackout
    if blackout is None:
        return CheckResult("corporate_action", True, "not checked")
    if getattr(blackout, "blocked", False):
        return CheckResult("corporate_action", False, blackout.detail)
    return CheckResult("corporate_action", True, blackout.detail or "clear")


def _check_duplicate(ctx, cfg) -> CheckResult:
    if ctx.duplicate_open:
        return CheckResult("duplicate", False, "identical structure already open")
    return CheckResult("duplicate", True, "no duplicate")


CHECKS: tuple[Callable, ...] = (
    _check_kill_switch,
    _check_halted,
    _check_market_window,
    _check_session_room,
    _check_defined_risk,
    _check_position_size,
    _check_portfolio_heat,
    _check_greeks,
    _check_concentration,
    _check_correlation,
    _check_liquidity,
    _check_time_to_expiry,
    _check_daily_loss,
    _check_drawdown,
    _check_rate_limits,
    _check_macro_blackout,
    _check_corporate_action,
    _check_duplicate,
)


def evaluate(ctx: GateContext, cfg) -> GateDecision:
    """Run every check. We do not short-circuit: knowing a trade failed four
    checks rather than one is worth the microseconds, and the full record is
    what makes the veto log useful."""
    results = tuple(check(ctx, cfg) for check in CHECKS)
    return GateDecision(approved=all(r.passed for r in results), checks=results)
