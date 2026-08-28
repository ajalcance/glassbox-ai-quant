"""HAR-RV — Corsi's Heterogeneous Autoregressive model of Realized Volatility.

Tomorrow's volatility is regressed on volatility measured over the last day, the
last week, and the last month:

    RV(t+1) = c + b_d*RV_d + b_w*RV_w + b_m*RV_m

Four parameters. It is deliberately the simplest thing that works, and it is
hard to beat — the reason it survives is that volatility genuinely clusters
across horizons, which is exactly what the three terms encode.

Volatility is forecastable in a way returns are not, which is why this is the
only price-series model in the system. Nothing here predicts direction.

Trained offline and frozen before the contest: fitting on the same days we then
trade would be exactly the look-ahead bias the rest of the design avoids.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


def realized_vol_series(closes: list[float]) -> list[float]:
    """Absolute log returns — a one-day realised volatility proxy."""
    from itertools import pairwise

    return [abs(math.log(b / a)) for a, b in pairwise(closes) if a > 0 and b > 0]


def _har_features(rv: list[float], i: int) -> list[float]:
    """Daily, weekly and monthly averages ending at index i (inclusive)."""
    daily = rv[i]
    weekly = sum(rv[max(0, i - 4) : i + 1]) / len(rv[max(0, i - 4) : i + 1])
    monthly = sum(rv[max(0, i - 21) : i + 1]) / len(rv[max(0, i - 21) : i + 1])
    return [daily, weekly, monthly]


@dataclass
class HarRv:
    coefficients: list[float] | None = None  # [c, b_d, b_w, b_m]
    n_samples: int = 0
    trained_at: str | None = None

    @property
    def is_trained(self) -> bool:
        return self.coefficients is not None

    def forecast(self, closes: list[float]) -> float | None:
        """Next-day realised volatility, or None when it cannot be estimated.

        None rather than a default: sizing skips the volatility budget when vol
        is unknown, which is safer than substituting a plausible-looking number.
        """
        if not self.is_trained:
            return None
        rv = realized_vol_series(closes)
        if len(rv) < 22:
            return None
        c, bd, bw, bm = self.coefficients
        d, w, m = _har_features(rv, len(rv) - 1)
        return max(0.0, c + bd * d + bw * w + bm * m)

    @classmethod
    def train(cls, series: list[list[float]]) -> HarRv:
        """Least-squares fit across one or more price histories."""
        import numpy as np

        from glassbox.clock import now_utc

        X, y = [], []
        for closes in series:
            rv = realized_vol_series(closes)
            for i in range(21, len(rv) - 1):
                X.append([1.0, *_har_features(rv, i)])
                y.append(rv[i + 1])
        if len(X) < 50:
            return cls(coefficients=None, n_samples=len(X))
        coefficients, *_ = np.linalg.lstsq(np.array(X), np.array(y), rcond=None)
        return cls(
            coefficients=[float(c) for c in coefficients],
            n_samples=len(X),
            trained_at=now_utc().isoformat(),
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "coefficients": self.coefficients,
                    "n_samples": self.n_samples,
                    "trained_at": self.trained_at,
                },
                indent=2,
            )
        )

    @classmethod
    def load(cls, path: str | Path) -> HarRv:
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls(
            coefficients=data.get("coefficients"),
            n_samples=data.get("n_samples", 0),
            trained_at=data.get("trained_at"),
        )
