"""Trade management — the triple barrier.

Alpaca does not support bracket or OCO orders on multi-leg positions, so every
exit has to be decided here. Each position is bounded by three barriers:

    profit  — bank the win before gamma turns against it
    stop    — cut before the defined loss is fully realised
    time    — leave if neither barrier is touched; a thesis has a shelf life

Whichever barrier is struck first both closes the position **and labels it**.
That is the triple-barrier method: the exit is the training signal, so the
meta-labeler learns from exactly the rule that governed the trade rather than
from some separately invented target.

Two additions on top of the barriers:

  * **Break-even**: once a position has earned most of its target, the stop
    moves to the entry price. A winner is never allowed to become a loser.
  * **Deadline**: everything is flattened before the submission cutoff. Holding
    unmanaged risk into a window you cannot supervise is not a strategy.

This is a pure function. No broker calls, no clock reads — `now` is passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from glassbox.structures import StructureKind

CREDIT_KINDS = frozenset(
    {
        StructureKind.BULL_PUT_SPREAD,
        StructureKind.BEAR_CALL_SPREAD,
        StructureKind.IRON_CONDOR,
    }
)


class Barrier(StrEnum):
    PROFIT = "profit"
    STOP = "stop"
    TIME = "time"
    BREAKEVEN = "breakeven"
    DEADLINE = "deadline"
    EXPIRY_RISK = "expiry_risk"
    NONE = "none"


class Action(StrEnum):
    HOLD = "hold"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class PositionView:
    """Everything needed to manage one open position."""

    position_id: str
    kind: StructureKind
    qty: int
    entry_price: float  # net per share: + debit paid, - credit received
    current_price: float  # net per share to close now, same convention
    max_loss_per_spread: float  # dollars, computed at entry
    opened_at: datetime
    horizon_hours: float
    hours_to_expiry: float
    peak_pnl: float = 0.0  # best unrealised P&L seen, dollars

    @property
    def is_credit(self) -> bool:
        return self.kind in CREDIT_KINDS

    @property
    def entry_premium(self) -> float:
        """Absolute premium per share paid (debit) or received (credit)."""
        return abs(self.entry_price)

    @property
    def unrealized_pnl(self) -> float:
        """Dollars, across all contracts.

        Both cases collapse to one subtraction. With the convention that a
        credit is negative and a debit positive, a credit sold at -1.20 and
        buyable back at -0.60 gives -0.60 - (-1.20) = +0.60, while a debit paid
        at 2.00 now worth 3.00 gives 3.00 - 2.00 = +1.00. Same formula, no
        special case — which is the payoff for keeping one sign convention all
        the way from the order router.
        """
        return (self.current_price - self.entry_price) * 100 * self.qty

    @property
    def max_profit(self) -> float:
        """Dollars. Meaningful for credit structures, where the premium
        received is the whole prize. A debit structure's ceiling depends on the
        spread width, which this view does not carry, so debit targets are
        expressed against the premium paid instead — see `profit_target`.
        """
        return self.entry_premium * 100 * self.qty


@dataclass(frozen=True, slots=True)
class ManageDecision:
    action: Action
    barrier: Barrier
    reason: str
    unrealized_pnl: float = 0.0
    label: int | None = None  # 1 profitable, 0 not — the meta-label

    @property
    def should_close(self) -> bool:
        return self.action is Action.CLOSE


def profit_target(view: PositionView, cfg) -> float:
    """Dollar profit that closes the position."""
    if view.is_credit:
        return view.max_profit * (cfg.manage.credit_profit_take_pct / 100)
    return view.entry_premium * 100 * view.qty * (cfg.manage.debit_profit_take_pct / 100)


def stop_level(view: PositionView, cfg) -> float:
    """Dollar loss (negative) that closes the position.

    Never wider than the structure's defined maximum loss — a stop beyond the
    worst case is not a stop.
    """
    if view.is_credit:
        loss = view.entry_premium * cfg.manage.stop_multiple_of_credit * 100 * view.qty
    else:
        loss = view.entry_premium * (cfg.manage.debit_stop_pct / 100) * 100 * view.qty
    return -min(loss, view.max_loss_per_spread * view.qty)


def evaluate_position(
    view: PositionView, cfg, now: datetime, deadline: datetime | None = None
) -> ManageDecision:
    """Decide whether to hold or close, and label the outcome if closing.

    Barriers are checked in order of severity: an obligation to be flat outranks
    a stop, which outranks a target. Checking the deadline first means a
    position never survives the cutoff because it happened to be profitable.
    """
    pnl = view.unrealized_pnl
    label = 1 if pnl > 0 else 0

    if deadline and now >= deadline:
        return ManageDecision(
            Action.CLOSE,
            Barrier.DEADLINE,
            f"submission deadline reached; flattening at ${pnl:+,.0f}",
            pnl,
            label,
        )

    if view.hours_to_expiry < cfg.manage.min_hours_to_expiry:
        return ManageDecision(
            Action.CLOSE,
            Barrier.EXPIRY_RISK,
            f"{view.hours_to_expiry:.0f}h to expiry < {cfg.manage.min_hours_to_expiry}h "
            f"— closing before gamma accelerates",
            pnl,
            label,
        )

    stop = stop_level(view, cfg)
    if pnl <= stop:
        return ManageDecision(
            Action.CLOSE,
            Barrier.STOP,
            f"stop hit: ${pnl:+,.0f} <= ${stop:+,.0f}",
            pnl,
            0,
        )

    target = profit_target(view, cfg)

    # Break-even: a position that earned most of its target may not be allowed
    # to round-trip into a loss.
    breakeven_armed = view.peak_pnl >= target * (cfg.manage.breakeven_trigger_pct / 100)
    if breakeven_armed and pnl <= 0:
        return ManageDecision(
            Action.CLOSE,
            Barrier.BREAKEVEN,
            f"break-even stop: peaked at ${view.peak_pnl:+,.0f}, now ${pnl:+,.0f}",
            pnl,
            label,
        )

    if pnl >= target:
        return ManageDecision(
            Action.CLOSE,
            Barrier.PROFIT,
            f"target hit: ${pnl:+,.0f} >= ${target:+,.0f}",
            pnl,
            1,
        )

    if now >= view.opened_at + timedelta(hours=view.horizon_hours):
        held = (now - view.opened_at).total_seconds() / 3600
        return ManageDecision(
            Action.CLOSE,
            Barrier.TIME,
            f"horizon elapsed ({held:.0f}h >= {view.horizon_hours:.0f}h) at ${pnl:+,.0f}",
            pnl,
            label,
        )

    return ManageDecision(
        Action.HOLD,
        Barrier.NONE,
        f"${pnl:+,.0f} between stop ${stop:+,.0f} and target ${target:+,.0f}"
        f"{'; break-even armed' if breakeven_armed else ''}",
        pnl,
        None,
    )
