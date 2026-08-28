"""Position sizing.

Two independent budgets are computed and the *smaller* wins:

  1. Fixed-fractional — a base risk R per trade, scaled by the meta-labeler's
     confidence that this particular signal is worth acting on.
  2. Volatility target — size so the position's expected P&L swing is
     comparable across regimes, rather than letting a quiet name and a wild one
     take identical risk.

Taking the min means a disagreement between the two always resolves toward
less risk. Sizing can only ever *reduce* exposure relative to the cap; it can
never authorise more than the fixed-fractional ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: int
    reason: str
    r_dollars: float
    meta_multiplier: float
    fixed_fractional_qty: int
    vol_target_qty: int

    @property
    def approved(self) -> bool:
        return self.qty > 0


def meta_multiplier(p: float, cfg) -> float:
    """Map P(profitable) to a size multiplier.

    Below the threshold we do not trade at all — a coin-flip signal sized small
    is still a coin flip. Above it, scale linearly to the ceiling and stop:
    we never lever up on high confidence, because confidence is the model's
    opinion and the cap is our policy.
    """
    floor_p = cfg.risk.min_meta_label_p
    if p < floor_p:
        return 0.0
    ceiling_p = 0.85
    if p >= ceiling_p:
        return cfg.sizing.meta_multiplier_ceiling
    span = ceiling_p - floor_p
    lo, hi = cfg.sizing.meta_multiplier_floor, cfg.sizing.meta_multiplier_ceiling
    return lo + (hi - lo) * ((p - floor_p) / span)


def size_position(
    equity: float,
    max_loss_per_spread: float,
    meta_label_p: float,
    cfg,
    underlying_vol: float | None = None,
    target_vol: float = 0.01,
    loss_streak: int = 0,
) -> SizingResult:
    """Contracts to trade, or zero with a reason.

    `max_loss_per_spread` is the defined, computed worst case in dollars —
    never an estimate. `underlying_vol` is daily realised vol as a decimal;
    when unavailable the volatility budget is skipped rather than guessed.
    """
    if max_loss_per_spread <= 0:
        raise ValueError(
            f"max_loss_per_spread must be positive, got {max_loss_per_spread}. "
            "A zero-risk position would make every downstream risk check meaningless."
        )

    mult = meta_multiplier(meta_label_p, cfg)
    if mult == 0.0:
        return SizingResult(
            0, f"meta-label p={meta_label_p:.2f} below {cfg.risk.min_meta_label_p}", 0.0, 0.0, 0, 0
        )

    r_dollars = equity * (cfg.risk.r_per_trade_pct / 100) * mult
    # A losing streak halves risk until the streak breaks — the anti-martingale
    # direction: reduce after losses, never increase.
    if loss_streak >= cfg.risk.loss_streak_half_size:
        r_dollars /= 2

    fixed_qty = int(r_dollars // max_loss_per_spread)

    if underlying_vol and underlying_vol > 0:
        vol_budget = equity * target_vol / underlying_vol * (cfg.risk.r_per_trade_pct / 100)
        vol_qty = int(vol_budget // max_loss_per_spread)
    else:
        vol_qty = fixed_qty  # no vol estimate: fall back rather than guess

    qty = min(fixed_qty, vol_qty)

    # Never exceed the hard per-position ceiling regardless of either budget.
    position_cap = equity * (cfg.risk.max_loss_per_position_pct / 100)
    qty = min(qty, int(position_cap // max_loss_per_spread))

    if qty < 1:
        return SizingResult(
            0,
            f"budget ${r_dollars:.0f} < one spread at ${max_loss_per_spread:.0f} risk",
            r_dollars,
            mult,
            fixed_qty,
            vol_qty,
        )
    binding = "volatility target" if vol_qty < fixed_qty else "fixed-fractional"
    return SizingResult(qty, f"{binding} binding", r_dollars, mult, fixed_qty, vol_qty)
