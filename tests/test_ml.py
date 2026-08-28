"""ML layer tests.

The behaviour that matters most is what these models do when they *don't* know
enough — abstaining beats answering confidently from four data points.
"""

import math

import pytest

from glassbox.config import load_config
from glassbox.ml.bandit import ThompsonBandit, VolRegime, classify_regime
from glassbox.ml.features import FEATURE_ORDER, SignalFeatures, build_features
from glassbox.ml.metalabel import MetaLabeler
from glassbox.ml.volforecast import HarRv, realized_vol_series
from glassbox.structures import StructureKind

CFG = load_config()


def features(**kw) -> SignalFeatures:
    base = {
        "confidence": 0.8,
        "expected_move_pct": 4.0,
        "implied_move_pct": 2.0,
        "edge_ratio": 2.0,
        "materiality": 0.9,
        "novelty": 1.0,
        "hours_to_expiry": 200.0,
        "realized_vol": 0.015,
        "spread_pct_of_mid": 2.0,
        "is_credit": 0.0,
    }
    base.update(kw)
    return SignalFeatures(**base)


# --- features -------------------------------------------------------------


def test_feature_vector_matches_declared_order():
    v = features().as_vector()
    assert len(v) == len(FEATURE_ORDER)
    assert v[FEATURE_ORDER.index("confidence")] == pytest.approx(0.8)
    assert v[FEATURE_ORDER.index("is_credit")] == 0.0


def test_feature_count_stays_well_under_sample_floor():
    """More features than samples is fitting noise with conviction."""
    assert len(FEATURE_ORDER) < CFG.ml.min_training_samples / 2


def test_missing_realized_vol_becomes_zero_not_a_guess():
    from types import SimpleNamespace

    f = build_features(
        view=SimpleNamespace(confidence=0.8, expected_move_pct=4.0, materiality=0.9),
        edge=SimpleNamespace(implied_move_pct=2.0, ratio=2.0),
        hours_to_expiry=200.0,
        realized_vol=None,
        spread_pct_of_mid=2.0,
        is_credit=True,
        novelty=0.9,
    )
    assert f.realized_vol == 0.0 and f.is_credit == 1.0


# --- meta-labeler: abstention is the headline behaviour -------------------


def test_untrained_model_abstains_and_says_so():
    m = MetaLabeler(min_samples=30)
    p, detail = m.predict(features(), fallback=0.77)
    assert p == pytest.approx(0.77)
    assert "abstaining" in detail and "0/30" in detail


def test_too_few_samples_produces_no_model():
    rows = [(features().as_vector(), i % 2) for i in range(10)]
    m = MetaLabeler.train(rows, min_samples=30)
    assert not m.is_trained
    assert m.predict(features(), fallback=0.6)[0] == pytest.approx(0.6)


def test_single_class_history_produces_no_model():
    """Thirty wins and no losses teaches nothing about what loses."""
    rows = [(features().as_vector(), 1) for _ in range(40)]
    m = MetaLabeler.train(rows, min_samples=30)
    assert not m.is_trained, "a model fitted on one class predicts with false certainty"


def test_trained_model_learns_a_separable_signal():
    rows = []
    for i in range(60):
        won = i % 2
        # High edge ratio wins, low loses — a signal the model should find.
        rows.append((features(edge_ratio=3.0 if won else 0.3).as_vector(), won))
    m = MetaLabeler.train(rows, min_samples=30)
    assert m.is_trained and m.n_samples == 60
    high, _ = m.predict(features(edge_ratio=3.0), fallback=0.5)
    low, _ = m.predict(features(edge_ratio=0.3), fallback=0.5)
    assert high > low, "model failed to learn a clearly separable signal"


def test_probability_is_reported_with_its_sample_count():
    rows = [(features(edge_ratio=3.0 if i % 2 else 0.3).as_vector(), i % 2) for i in range(60)]
    m = MetaLabeler.train(rows, min_samples=30)
    _, detail = m.predict(features(), fallback=0.5)
    assert "n=60" in detail


# --- meta-labeler persistence --------------------------------------------


def test_missing_model_file_means_abstain_not_crash(tmp_path):
    m = MetaLabeler.load(tmp_path / "nope.pkl")
    assert not m.is_trained
    assert m.predict(features(), fallback=0.5)[0] == pytest.approx(0.5)


def test_corrupt_model_file_degrades_to_abstaining(tmp_path):
    """A corrupt artifact must not take the trader down at the open."""
    path = tmp_path / "metalabel.pkl"
    path.write_bytes(b"not a pickle")
    assert not MetaLabeler.load(path).is_trained


def test_changed_feature_set_invalidates_a_stored_model(tmp_path):
    """Stored coefficients no longer mean what they did; abstain rather than
    apply them to a different feature vector."""
    rows = [(features(edge_ratio=3.0 if i % 2 else 0.3).as_vector(), i % 2) for i in range(60)]
    m = MetaLabeler.train(rows, min_samples=30)
    m.feature_order = ("confidence", "something_else")
    path = tmp_path / "m.pkl"
    m.save(path)
    assert not MetaLabeler.load(path).is_trained


def test_round_trip_preserves_predictions(tmp_path):
    rows = [(features(edge_ratio=3.0 if i % 2 else 0.3).as_vector(), i % 2) for i in range(60)]
    m = MetaLabeler.train(rows, min_samples=30)
    path = tmp_path / "m.pkl"
    m.save(path)
    loaded = MetaLabeler.load(path)
    assert loaded.is_trained
    assert loaded.predict(features(), 0.5)[0] == pytest.approx(m.predict(features(), 0.5)[0])


# --- bandit ---------------------------------------------------------------


def test_regime_classification():
    bounds = CFG.ml.vol_regime_bounds
    assert classify_regime(0.005, bounds) is VolRegime.LOW
    assert classify_regime(0.015, bounds) is VolRegime.NORMAL
    assert classify_regime(0.030, bounds) is VolRegime.HIGH
    assert classify_regime(None, bounds) is VolRegime.NORMAL, "unknown vol is not its own bucket"


def test_bandit_only_chooses_among_eligible_structures(store):
    """The edge test decides the view; the bandit only picks how to express it."""
    b = ThompsonBandit(store, seed=0)
    eligible = (StructureKind.BULL_PUT_SPREAD, StructureKind.IRON_CONDOR)
    for _ in range(20):
        assert b.select(eligible, VolRegime.NORMAL).kind in eligible


def test_bandit_rejects_an_empty_arm_set(store):
    with pytest.raises(ValueError, match="no eligible"):
        ThompsonBandit(store, seed=0).select((), VolRegime.NORMAL)


def test_posterior_updates_shift_selection_toward_the_winner(store):
    b = ThompsonBandit(store, seed=7)
    eligible = (StructureKind.BULL_PUT_SPREAD, StructureKind.IRON_CONDOR)
    for _ in range(30):
        b.update(StructureKind.BULL_PUT_SPREAD, VolRegime.NORMAL, won=True)
        b.update(StructureKind.IRON_CONDOR, VolRegime.NORMAL, won=False)
    picks = [b.select(eligible, VolRegime.NORMAL).kind for _ in range(50)]
    winner_share = picks.count(StructureKind.BULL_PUT_SPREAD) / len(picks)
    assert winner_share > 0.8, f"bandit ignored a clear winner ({winner_share:.0%})"


def test_exploration_survives_a_strong_prior(store):
    """A wide posterior keeps being sampled — that is the point of Thompson
    sampling over a greedy rule."""
    b = ThompsonBandit(store, seed=3)
    eligible = (StructureKind.BULL_PUT_SPREAD, StructureKind.IRON_CONDOR)
    for _ in range(6):
        b.update(StructureKind.BULL_PUT_SPREAD, VolRegime.NORMAL, won=True)
    picks = [b.select(eligible, VolRegime.NORMAL).kind for _ in range(60)]
    assert StructureKind.IRON_CONDOR in picks, "no exploration of the untried arm"


def test_regimes_are_learned_independently(store):
    b = ThompsonBandit(store, seed=1)
    for _ in range(20):
        b.update(StructureKind.IRON_CONDOR, VolRegime.HIGH, won=True)
    assert b.store.bandit_posteriors(str(VolRegime.HIGH))
    assert not b.store.bandit_posteriors(str(VolRegime.LOW))


def test_summary_reports_posteriors_for_the_dashboard(store):
    b = ThompsonBandit(store, seed=1)
    b.update(StructureKind.BULL_PUT_SPREAD, VolRegime.NORMAL, won=True)
    b.update(StructureKind.BULL_PUT_SPREAD, VolRegime.NORMAL, won=False)
    row = b.summary()[0]
    assert row["pulls"] == 2 and 0 < row["mean"] < 1


# --- HAR-RV ---------------------------------------------------------------


def _trending_vol_series(n=300, seed=0):
    import random

    rng = random.Random(seed)
    price, out, vol = 100.0, [100.0], 0.01
    for _ in range(n):
        vol = 0.9 * vol + 0.1 * rng.uniform(0.005, 0.03)  # clustered volatility
        price *= math.exp(rng.gauss(0, vol))
        out.append(price)
    return out


def test_untrained_forecaster_returns_none_not_a_default():
    assert HarRv().forecast(_trending_vol_series()) is None


def test_insufficient_history_returns_none():
    m = HarRv.train([_trending_vol_series()])
    assert m.forecast([100.0, 101.0, 102.0]) is None


def test_har_rv_trains_and_forecasts_positive_vol():
    m = HarRv.train([_trending_vol_series(seed=i) for i in range(4)])
    assert m.is_trained and len(m.coefficients) == 4
    forecast = m.forecast(_trending_vol_series(seed=99))
    assert forecast is not None and 0 < forecast < 0.5


def test_har_rv_loadings_are_economically_sensible():
    """Volatility clusters, so recent volatility should load positively."""
    m = HarRv.train([_trending_vol_series(seed=i) for i in range(6)])
    _, daily, weekly, monthly = m.coefficients
    assert daily + weekly + monthly > 0, "no persistence learned"


def test_realized_vol_series_ignores_bad_prices():
    assert realized_vol_series([100.0, 0.0, 101.0]) == []
    assert len(realized_vol_series([100.0, 101.0, 102.0])) == 2


def test_har_rv_round_trip(tmp_path):
    m = HarRv.train([_trending_vol_series(seed=i) for i in range(4)])
    path = tmp_path / "harrv.json"
    m.save(path)
    loaded = HarRv.load(path)
    assert loaded.is_trained
    series = _trending_vol_series(seed=42)
    assert loaded.forecast(series) == pytest.approx(m.forecast(series))


def test_corrupt_vol_model_degrades_to_untrained(tmp_path):
    path = tmp_path / "harrv.json"
    path.write_text("{not json")
    assert not HarRv.load(path).is_trained
