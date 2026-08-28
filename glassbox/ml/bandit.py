"""Thompson sampling over defined-risk structures.

The edge test decides *what view to express*; this decides *how*. Each
structure is an arm with a Beta posterior over its win rate, and a draw from
each posterior picks the winner — so an arm that has done well is played more
often, while an arm with little history keeps getting sampled precisely because
its posterior is still wide.

This is the correct algorithm class for the sample size. A policy-gradient
method needs on the order of 10^5 episodes; a contest produces tens of trades.
Beta-Bernoulli Thompson sampling converges usefully in tens of pulls, and it is
honest about what it does not know rather than confidently wrong.

Regimes are coarse — three volatility buckets — because the number of cells has
to stay small enough that each one accumulates real observations. A finely
contextual bandit here would just be many arms that have each been pulled once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from glassbox.structures import StructureKind


class VolRegime(StrEnum):
    LOW = "low_vol"
    NORMAL = "normal_vol"
    HIGH = "high_vol"


def classify_regime(realized_vol: float | None, bounds: list[float]) -> VolRegime:
    """Bucket by realised volatility. Unknown vol is treated as normal, which
    is the bucket that carries the most observations."""
    if realized_vol is None:
        return VolRegime.NORMAL
    low, high = bounds[0], bounds[1]
    if realized_vol < low:
        return VolRegime.LOW
    if realized_vol >= high:
        return VolRegime.HIGH
    return VolRegime.NORMAL


@dataclass(frozen=True, slots=True)
class ArmChoice:
    kind: StructureKind
    regime: VolRegime
    sampled_value: float
    posterior: tuple[float, float]
    pulls: int
    detail: str


@dataclass
class ThompsonBandit:
    store: object
    prior_alpha: float = 1.0
    prior_beta: float = 1.0
    seed: int | None = None
    _rng: object = field(default=None, repr=False)

    @property
    def rng(self):
        if self._rng is None:
            import numpy as np

            self._rng = np.random.default_rng(self.seed)
        return self._rng

    def select(self, eligible: tuple[StructureKind, ...], regime: VolRegime) -> ArmChoice:
        """Draw once from each eligible arm's posterior; play the best draw.

        Only structures the edge test proposed are considered — the bandit
        chooses among valid expressions of a view, never the view itself.
        """
        if not eligible:
            raise ValueError("no eligible structures to choose between")

        posteriors = self.store.bandit_posteriors(str(regime))
        best, best_value, best_stats = None, -1.0, (self.prior_alpha, self.prior_beta, 0)

        for kind in eligible:
            alpha, beta, pulls = posteriors.get(str(kind), (self.prior_alpha, self.prior_beta, 0))
            draw = float(self.rng.beta(alpha, beta))
            if draw > best_value:
                best, best_value, best_stats = kind, draw, (alpha, beta, pulls)

        alpha, beta, pulls = best_stats
        mean = alpha / (alpha + beta)
        return ArmChoice(
            kind=best,
            regime=regime,
            sampled_value=best_value,
            posterior=(alpha, beta),
            pulls=pulls,
            detail=(
                f"{best} in {regime}: drew {best_value:.3f} from Beta({alpha:.0f},{beta:.0f}) "
                f"[mean {mean:.2f}, {pulls} pulls]"
                + ("" if len(eligible) > 1 else " — only eligible structure")
            ),
        )

    def update(self, kind: StructureKind, regime: VolRegime, won: bool) -> None:
        """One Bernoulli observation from a closed position."""
        self.store.update_bandit(str(kind), str(regime), won)

    def summary(self) -> list[dict]:
        """Posterior state for the dashboard and the nightly report."""
        out = []
        for row in self.store.all_bandit_state():
            alpha, beta = row["alpha"], row["beta"]
            out.append(
                {
                    "arm": row["arm"],
                    "regime": row["regime"],
                    "alpha": alpha,
                    "beta": beta,
                    "pulls": row["pulls"],
                    "mean": alpha / (alpha + beta),
                }
            )
        return out
