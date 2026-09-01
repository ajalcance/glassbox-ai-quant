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

On top of those three sit obligations — conditions under which we exit
regardless of where P&L happens to be:

  * **Break-even**: once a position has earned most of its target, the stop
    moves to the entry price. A winner is never allowed to become a loser.
  * **Deadline**: everything is flattened before the submission cutoff. Holding
    unmanaged risk into a window you cannot supervise is not a strategy.
  * **Expiry risk**: out before gamma accelerates into the final day.
  * **Macro risk**: short premium is closed before a scheduled release. The
    gate refuses to *open* one into an event; permitting the *hold* would guard
    the front door and leave the back one open.
  * **Thesis complete**: the underlying has travelled the distance the entry
    thesis predicted. The forecast move has happened, so there is nothing left
    to wait for — a different question from whether P&L reached its target, and
    on a debit position whose implied volatility collapsed the two disagree.
  * **Thesis broken**: later news reverses the view the position was opened on.
    Handled in the orchestrator, since it is triggered by an arriving story
    rather than by a tick.

An entry thesis should have an expiry condition, not only an expiry date. The
last two supply the condition.

**Position size never changes once opened.** The manager is deliberately binary:
a position is held whole or closed whole. Scaling out would be the obvious
addition and it is not available to us — at a 0.5% risk budget a typical spread
is one contract, and half a contract does not exist. Scaling in is available but
declined: adding to an open thesis raises risk on a view already in the market,
which the failure literature identifies as a principal way retail systems end.

Risk on an open position is therefore reduced through **exits, not size** — the
break-even stop, and the obligation barriers below. Size adapts before entry
instead, through the budget tapers in `sizing.py`.

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
    MACRO_RISK = "macro_risk"
    THESIS_BROKEN = "thesis_broken"
    THESIS_COMPLETE = "thesis_complete"
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
    # The thesis this position was opened on, for the completion check.
    thesis_direction: str = ""
    thesis_move_pct: float = 0.0
    entry_spot: float = 0.0
    current_spot: float = 0.0

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
    def move_since_entry_pct(self) -> float:
        """Signed percentage move in the underlying since entry."""
        if self.entry_spot <= 0 or self.current_spot <= 0:
            return 0.0
        return (self.current_spot - self.entry_spot) / self.entry_spot * 100

    def thesis_move_achieved(self, cfg) -> bool:
        """Has the underlying travelled the distance the thesis predicted?

        Direction matters: a stock that moved the predicted distance the *wrong*
        way has not completed the thesis, it has falsified it — and that is the
        stop's job, not this one.
        """
        if self.thesis_move_pct <= 0 or self.entry_spot <= 0:
            return False
        moved = self.move_since_entry_pct
        required = self.thesis_move_pct * cfg.manage.thesis_complete_fraction
        if self.thesis_direction == "up":
            return moved >= required
        if self.thesis_direction == "down":
            return -moved >= required
        # A vol_only thesis predicts magnitude without a sign; either side counts.
        return abs(moved) >= required

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
    view: PositionView,
    cfg,
    now: datetime,
    deadline: datetime | None = None,
    macro_window=None,
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

    # The gate refuses to *open* short premium into a scheduled release. Holding
    # one through the release is the identical exposure — a credit spread opened
    # Monday with a two-day horizon sits straight through Tuesday's ISM print.
    # Blocking the entry while permitting the hold would guard the front door
    # and leave the back one open.
    if macro_window is not None and getattr(macro_window, "active", False) and view.is_credit:
        return ManageDecision(
            Action.CLOSE,
            Barrier.MACRO_RISK,
            f"closing short premium before {macro_window.detail}",
            pnl,
            label,
        )

    # Completion: the underlying has travelled the distance we predicted. The
    # forecast move has happened, so there is nothing further to wait for —
    # which is a different question from whether P&L reached its target, and on
    # a debit position whose implied volatility collapsed the two can disagree.
    if cfg.manage.exit_on_thesis_complete and view.thesis_move_achieved(cfg):
        travelled = view.move_since_entry_pct
        return ManageDecision(
            Action.CLOSE,
            Barrier.THESIS_COMPLETE,
            f"predicted {view.thesis_move_pct:.2f}%, underlying moved "
            f"{travelled:.2f}% — the forecast move has happened",
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

    horizon_elapsed = now >= view.opened_at + timedelta(hours=view.horizon_hours)
    if horizon_elapsed and pnl > 0:
        # The bell banks winners; it never realises losers. A thesis that has
        # run its course in profit has nothing left to wait for. One that ran
        # its course at a loss is NOT dumped for calendar reasons (changed
        # 1 Sep on operator instruction): a defined-risk structure's downside
        # is already capped by the wing, so it rides — bounded by the stop
        # intraday, the wing overnight, macro_risk for credits, expiry_risk
        # inside 24h, and the deadline flatten. Overnight carry of an
        # unresolved thesis is a deliberate policy, not an accident.
        held = (now - view.opened_at).total_seconds() / 3600
        return ManageDecision(
            Action.CLOSE,
            Barrier.TIME,
            f"horizon elapsed ({held:.0f}h >= {view.horizon_hours:.0f}h) — "
            f"banking ${pnl:+,.0f}",
            pnl,
            label,
        )

    return ManageDecision(
        Action.HOLD,
        Barrier.NONE,
        f"${pnl:+,.0f} between stop ${stop:+,.0f} and target ${target:+,.0f}"
        f"{'; break-even armed' if breakeven_armed else ''}"
        f"{'; horizon elapsed — riding a bounded loss' if horizon_elapsed else ''}",
        pnl,
        None,
    )
