"""Meta-labeling — separating the *side* decision from the *size* decision.

López de Prado's construction: a primary model decides direction (here, the
edge test), and a secondary model decides how much to trust it. The secondary
model's probability drives position size, never direction. That separation is
what makes ML safe to add to a rules-based system — a model failure shrinks a
position, it does not invert a trade.

Two deliberate constraints:

  * **Regularised logistic regression, not gradient boosting.** A contest
    produces tens of labelled trades. A boosted tree ensemble on forty rows is
    noise fitted with conviction; a penalised linear model degrades gracefully
    and its coefficients can be read and sanity-checked by a human.

  * **It abstains below a minimum sample count.** Under that threshold the
    analyst's own confidence is returned instead. A model that says "I do not
    have enough evidence" is more useful than one that always answers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from glassbox.ml.features import FEATURE_ORDER, SignalFeatures


@dataclass
class MetaLabeler:
    model: object | None = None
    n_samples: int = 0
    feature_order: tuple[str, ...] = FEATURE_ORDER
    min_samples: int = 30
    trained_at: str | None = None

    @property
    def is_trained(self) -> bool:
        return self.model is not None and self.n_samples >= self.min_samples

    def predict(self, features: SignalFeatures, fallback: float) -> tuple[float, str]:
        """P(this signal is profitable), and why that number was produced.

        `fallback` is the analyst's confidence — the honest stand-in while the
        model is still abstaining.
        """
        if not self.is_trained:
            return fallback, (
                f"meta-labeler abstaining ({self.n_samples}/{self.min_samples} samples); "
                f"using analyst confidence"
            )
        import numpy as np

        p = float(self.model.predict_proba(np.array([features.as_vector()]))[0][1])
        return p, f"meta-labeler p={p:.3f} (n={self.n_samples})"

    # -- training ---------------------------------------------------------
    @classmethod
    def train(cls, rows: list[tuple[list[float], int]], min_samples: int = 30) -> MetaLabeler:
        """Fit on (feature vector, label) pairs from closed positions."""
        from glassbox.clock import now_utc

        if len(rows) < min_samples:
            return cls(model=None, n_samples=len(rows), min_samples=min_samples)

        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        X = np.array([r[0] for r in rows], dtype=float)
        y = np.array([r[1] for r in rows], dtype=int)
        if len(set(y.tolist())) < 2:
            # All wins or all losses: nothing to separate, and a model fitted
            # here would predict one class with false certainty.
            return cls(model=None, n_samples=len(rows), min_samples=min_samples)

        # C is small on purpose: heavy regularisation is the whole defence
        # against overfitting a few dozen rows.
        model = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=0.3, max_iter=2000, class_weight="balanced"),
        ).fit(X, y)
        return cls(
            model=model,
            n_samples=len(rows),
            min_samples=min_samples,
            trained_at=now_utc().isoformat(),
        )

    # -- persistence ------------------------------------------------------
    def save(self, path: str | Path) -> None:
        import pickle

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(
                {
                    "model": self.model,
                    "n_samples": self.n_samples,
                    "feature_order": self.feature_order,
                    "min_samples": self.min_samples,
                    "trained_at": self.trained_at,
                },
                f,
            )
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "n_samples": self.n_samples,
                    "trained": self.is_trained,
                    "feature_order": list(self.feature_order),
                    "trained_at": self.trained_at,
                },
                indent=2,
            )
        )

    @classmethod
    def load(cls, path: str | Path, min_samples: int = 30) -> MetaLabeler:
        """A missing or unreadable model is not an error — it means abstain."""
        import pickle

        path = Path(path)
        if not path.exists():
            return cls(model=None, n_samples=0, min_samples=min_samples)
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
        except Exception:  # noqa: BLE001 -- a corrupt model must degrade to
            # abstaining, never take the trader down at the open.
            return cls(model=None, n_samples=0, min_samples=min_samples)
        if tuple(data.get("feature_order", ())) != FEATURE_ORDER:
            # Features changed since training; the stored coefficients no longer
            # mean what they used to. Abstain rather than mis-apply them.
            return cls(model=None, n_samples=0, min_samples=min_samples)
        return cls(
            model=data["model"],
            n_samples=data["n_samples"],
            feature_order=tuple(data["feature_order"]),
            min_samples=data.get("min_samples", min_samples),
            trained_at=data.get("trained_at"),
        )
