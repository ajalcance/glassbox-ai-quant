"""Scoring the analyst against what actually happened.

Every threshold in the entry logic is expressed as a ratio of the analyst's
expected move to something else. All of them therefore rest on an unexamined
assumption: that the analyst's numbers mean what they appear to mean. If it
systematically estimates twice the move that materialises, then a ratio of 1.3
is really 0.65 and every threshold is calibrated to the wrong centre.

This records each estimate at the moment it is made and scores it once the
horizon has elapsed. Crucially it records **every** estimate, not only the ones
that became trades: vetoed and untraded signals are the large majority of the
sample and are exactly as informative about whether the model over-estimates.
That is what makes a dry run produce real calibration data without placing a
single order.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import timedelta

from glassbox.clock import now_utc


@dataclass(frozen=True, slots=True)
class Calibration:
    n: int
    mean_expected: float
    mean_actual: float
    median_ratio: float
    direction_accuracy: float | None
    directional_n: int

    @property
    def bias(self) -> float:
        """Expected divided by actual. Above 1 means the analyst over-estimates."""
        return self.mean_expected / self.mean_actual if self.mean_actual else float("inf")

    @property
    def suggested_centre(self) -> float:
        """Where a fairly-priced signal should score once bias is removed.

        If the analyst over-estimates by 2x, an expected/implied ratio of 2.0 is
        really a fair-value signal, and thresholds anchored at 1.0 are measuring
        the model's optimism rather than a mispricing.
        """
        return self.bias

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "mean_expected_pct": round(self.mean_expected, 3),
            "mean_actual_pct": round(self.mean_actual, 3),
            "bias": round(self.bias, 3),
            "median_ratio": round(self.median_ratio, 3),
            "direction_accuracy": (
                round(self.direction_accuracy, 3) if self.direction_accuracy is not None else None
            ),
            "directional_n": self.directional_n,
        }


def record(store, *, signal_id, symbol, spot, view, implied_move_pct, now=None) -> str:
    """Log an estimate at the moment it is made."""
    now = now or now_utc()
    prediction_id = f"{signal_id}@{now:%Y%m%dT%H%M%S}"
    store.record_prediction(
        prediction_id,
        signal_id=signal_id,
        symbol=symbol,
        predicted_at=now.isoformat(),
        spot_at_prediction=float(spot),
        expected_move_pct=float(view.expected_move_pct),
        direction=str(view.direction),
        confidence=float(view.confidence),
        horizon_hours=float(view.horizon_hours),
        implied_move_pct=float(implied_move_pct),
        resolve_after=(now + timedelta(hours=float(view.horizon_hours))).isoformat(),
    )
    return prediction_id


def resolve_due(store, market_data, now=None, limit: int = 200) -> int:
    """Score every prediction whose horizon has elapsed. Returns how many."""
    from datetime import datetime

    now = now or now_utc()
    scored = 0
    for row in store.due_predictions(now.isoformat(), limit):
        start = datetime.fromisoformat(row["predicted_at"])
        end = datetime.fromisoformat(row["resolve_after"])
        measured = market_data.measure_move(row["symbol"], start, end)
        if measured is None:
            # Unmeasurable now may be measurable later — bars arrive late, and a
            # weekend gap simply means the window has not filled yet. Left
            # unresolved rather than scored as zero movement, which would drag
            # the whole calibration toward "the analyst over-estimates".
            continue
        signed, absolute = measured
        store.resolve_prediction(row["prediction_id"], absolute, signed)
        scored += 1
    return scored


def calibration(store) -> Calibration | None:
    """How the analyst's estimates compare with what happened."""
    rows = store.resolved_predictions()
    if not rows:
        return None

    expected = [float(r["expected_move_pct"]) for r in rows]
    actual = [float(r["actual_move_pct"]) for r in rows]
    ratios = [e / a for e, a in zip(expected, actual, strict=True) if a > 0]

    directional = [r for r in rows if r["direction"] in ("up", "down")]
    hits = sum(
        1 for r in directional if (float(r["actual_signed_pct"]) > 0) == (r["direction"] == "up")
    )

    return Calibration(
        n=len(rows),
        mean_expected=statistics.mean(expected),
        mean_actual=statistics.mean(actual),
        median_ratio=statistics.median(ratios) if ratios else float("inf"),
        direction_accuracy=(hits / len(directional)) if directional else None,
        directional_n=len(directional),
    )
