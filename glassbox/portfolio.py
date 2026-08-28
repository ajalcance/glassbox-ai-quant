"""Portfolio-level risk aggregation.

Per-trade limits are necessary but not sufficient: five "different" trades can
be one trade in disguise. This module answers the portfolio questions — how
much total risk is on, how directional is the book, and is it concentrated.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Greeks:
    delta_dollars: float = 0.0
    vega: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0

    def __add__(self, other: Greeks) -> Greeks:
        return Greeks(
            self.delta_dollars + other.delta_dollars,
            self.vega + other.vega,
            self.gamma + other.gamma,
            self.theta + other.theta,
        )


@dataclass(frozen=True, slots=True)
class PortfolioState:
    heat: float  # sum of max-loss across live positions
    greeks: Greeks
    positions_by_underlying: dict[str, int]
    open_position_count: int

    def heat_pct(self, equity: float) -> float:
        return 100 * self.heat / equity if equity > 0 else float("inf")


def snapshot(store, greeks_by_position: dict[str, Greeks] | None = None) -> PortfolioState:
    """Build the current portfolio picture from local state.

    Greeks are supplied by the caller (they come from live option snapshots);
    when absent the Greeks bands simply read as zero rather than being faked.
    """
    greeks_by_position = greeks_by_position or {}
    total = Greeks()
    by_underlying: dict[str, int] = {}
    heat = 0.0
    count = 0

    for row in store.open_positions():
        count += 1
        heat += float(row["max_loss"])
        underlying = row["underlying"]
        by_underlying[underlying] = by_underlying.get(underlying, 0) + 1
        total = total + greeks_by_position.get(row["position_id"], Greeks())

    return PortfolioState(heat, total, by_underlying, count)


def correlated_exposure(
    positions_by_underlying: dict[str, int],
    candidate: str,
    correlations: dict[tuple[str, str], float],
    threshold: float = 0.7,
) -> int:
    """How many existing positions sit in names highly correlated to `candidate`.

    Two positions in names that move together are one position with extra
    commission, so they count against concentration even though the tickers
    differ.
    """
    count = 0
    for underlying, n in positions_by_underlying.items():
        if underlying == candidate:
            continue
        key = (min(underlying, candidate), max(underlying, candidate))
        if abs(correlations.get(key, 0.0)) >= threshold:
            count += n
    return count


