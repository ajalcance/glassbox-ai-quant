"""Feature extraction for the meta-labeler.

Kept deliberately small. With tens of labelled trades, every extra feature buys
variance rather than signal — the feature count has to stay well under the
sample count or the model is fitting noise with conviction.

Features are all things known *before* the trade, and each is stamped from the
decision context rather than recomputed later, so a training row can never
contain information the live decision did not have.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Order matters: it defines the model's input vector and is persisted with the
# model so a retrained model and a stale feature order can never be paired.
FEATURE_ORDER = (
    "confidence",
    "expected_move_pct",
    "implied_move_pct",
    "edge_ratio",
    "materiality",
    "novelty",
    "hours_to_expiry",
    "realized_vol",
    "spread_pct_of_mid",
    "is_credit",
)


@dataclass(frozen=True, slots=True)
class SignalFeatures:
    confidence: float
    expected_move_pct: float
    implied_move_pct: float
    edge_ratio: float
    materiality: float
    novelty: float
    hours_to_expiry: float
    realized_vol: float
    spread_pct_of_mid: float
    is_credit: float  # 1.0 credit, 0.0 debit

    def as_vector(self) -> list[float]:
        d = asdict(self)
        return [float(d[name]) for name in FEATURE_ORDER]

    def as_dict(self) -> dict:
        return asdict(self)


def build_features(
    *,
    view,
    edge,
    hours_to_expiry: float,
    realized_vol: float | None,
    spread_pct_of_mid: float,
    is_credit: bool,
    novelty: float = 1.0,
) -> SignalFeatures:
    return SignalFeatures(
        confidence=view.confidence,
        expected_move_pct=view.expected_move_pct,
        implied_move_pct=edge.implied_move_pct,
        edge_ratio=edge.ratio,
        materiality=view.materiality,
        novelty=novelty,
        hours_to_expiry=hours_to_expiry,
        realized_vol=realized_vol if realized_vol is not None else 0.0,
        spread_pct_of_mid=spread_pct_of_mid,
        is_credit=1.0 if is_credit else 0.0,
    )
