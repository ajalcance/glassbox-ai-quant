"""Prediction scoring tests.

Every entry threshold is a ratio involving the analyst's expected move, so all
of them rest on the assumption that its numbers mean what they appear to. These
tests cover the machinery that checks that assumption.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from glassbox import predictions

NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def view(expected=4.0, direction="up", horizon=6.0, confidence=0.8):
    return SimpleNamespace(
        expected_move_pct=expected,
        direction=direction,
        horizon_hours=horizon,
        confidence=confidence,
    )


class FakeData:
    """Returns a fixed measured move, or None for unmeasurable."""

    def __init__(self, signed=None):
        self.signed = signed
        self.calls = []

    def measure_move(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.signed is None:
            return None
        return self.signed, abs(self.signed)


def record(store, expected=4.0, direction="up", horizon=6.0, signal="AAPL-1"):
    return predictions.record(
        store,
        signal_id=signal,
        symbol="AAPL",
        spot=230.0,
        view=view(expected, direction, horizon),
        implied_move_pct=2.0,
        now=NOW,
    )


# --- recording ------------------------------------------------------------


def test_prediction_is_recorded_with_a_resolve_time(store):
    record(store, horizon=6.0)
    row = store.due_predictions((NOW + timedelta(hours=7)).isoformat())[0]
    assert row["symbol"] == "AAPL"
    assert row["expected_move_pct"] == pytest.approx(4.0)
    assert row["resolve_after"] == (NOW + timedelta(hours=6)).isoformat()


def test_prediction_is_not_due_before_its_horizon(store):
    record(store, horizon=6.0)
    assert store.due_predictions((NOW + timedelta(hours=2)).isoformat()) == []


def test_untraded_predictions_are_still_recorded(store):
    """Untraded signals are most of the sample and just as informative."""
    record(store, signal="AAPL-1")
    row = store.due_predictions((NOW + timedelta(hours=9)).isoformat())[0]
    assert row["traded"] == 0


def test_trading_marks_the_prediction(store):
    record(store, signal="AAPL-1")
    store.mark_prediction_traded("AAPL-1")
    row = store.due_predictions((NOW + timedelta(hours=9)).isoformat())[0]
    assert row["traded"] == 1


# --- resolution -----------------------------------------------------------


def test_elapsed_prediction_is_scored(store):
    record(store, horizon=6.0)
    data = FakeData(signed=2.5)
    assert predictions.resolve_due(store, data, now=NOW + timedelta(hours=7)) == 1
    resolved = store.resolved_predictions()[0]
    assert resolved["actual_move_pct"] == pytest.approx(2.5)
    assert resolved["actual_signed_pct"] == pytest.approx(2.5)


def test_a_scored_prediction_is_not_scored_again(store):
    record(store, horizon=6.0)
    data = FakeData(signed=2.5)
    later = NOW + timedelta(hours=7)
    assert predictions.resolve_due(store, data, now=later) == 1
    assert predictions.resolve_due(store, data, now=later) == 0


def test_unmeasurable_move_is_left_unresolved_not_scored_zero(store):
    """Bars arrive late and weekends leave the window unfilled. Scoring those as
    zero movement would drag the whole calibration toward 'over-estimates'."""
    record(store, horizon=6.0)
    assert predictions.resolve_due(store, FakeData(signed=None), now=NOW + timedelta(hours=7)) == 0
    assert store.resolved_predictions() == []
    # Still due, so a later run can score it once the data exists.
    assert store.due_predictions((NOW + timedelta(hours=9)).isoformat())


def test_downward_move_is_measured_signed(store):
    record(store, direction="down", horizon=6.0)
    predictions.resolve_due(store, FakeData(signed=-3.0), now=NOW + timedelta(hours=7))
    resolved = store.resolved_predictions()[0]
    assert resolved["actual_signed_pct"] == pytest.approx(-3.0)
    assert resolved["actual_move_pct"] == pytest.approx(3.0)


# --- calibration ----------------------------------------------------------


def test_no_data_means_no_calibration(store):
    assert predictions.calibration(store) is None


def test_over_estimation_is_detected(store):
    """The finding that would move every threshold: the analyst predicts 4%,
    the market delivers 1%, so a ratio of 1.3 is really 0.33."""
    for i in range(5):
        predictions.record(
            store,
            signal_id=f"S-{i}",
            symbol="AAPL",
            spot=230.0,
            view=view(expected=4.0),
            implied_move_pct=2.0,
            now=NOW,
        )
    predictions.resolve_due(store, FakeData(signed=1.0), now=NOW + timedelta(hours=7))

    calib = predictions.calibration(store)
    assert calib.n == 5
    assert calib.mean_expected == pytest.approx(4.0)
    assert calib.mean_actual == pytest.approx(1.0)
    assert calib.bias == pytest.approx(4.0), "analyst over-estimates fourfold"
    assert calib.suggested_centre == pytest.approx(4.0)


def test_well_calibrated_analyst_scores_near_one(store):
    for i in range(4):
        predictions.record(
            store,
            signal_id=f"S-{i}",
            symbol="AAPL",
            spot=230.0,
            view=view(expected=2.0),
            implied_move_pct=2.0,
            now=NOW,
        )
    predictions.resolve_due(store, FakeData(signed=2.0), now=NOW + timedelta(hours=7))
    assert predictions.calibration(store).bias == pytest.approx(1.0)


def test_direction_accuracy_is_tracked(store):
    predictions.record(
        store,
        signal_id="up-right",
        symbol="A",
        spot=100.0,
        view=view(direction="up"),
        implied_move_pct=2.0,
        now=NOW,
    )
    predictions.resolve_due(store, FakeData(signed=3.0), now=NOW + timedelta(hours=7))
    calib = predictions.calibration(store)
    assert calib.direction_accuracy == pytest.approx(1.0) and calib.directional_n == 1


def test_wrong_direction_is_counted_as_a_miss(store):
    predictions.record(
        store,
        signal_id="up-wrong",
        symbol="A",
        spot=100.0,
        view=view(direction="up"),
        implied_move_pct=2.0,
        now=NOW,
    )
    predictions.resolve_due(store, FakeData(signed=-3.0), now=NOW + timedelta(hours=7))
    assert predictions.calibration(store).direction_accuracy == pytest.approx(0.0)


def test_vol_only_calls_are_excluded_from_direction_accuracy(store):
    """A vol_only view makes no directional claim, so scoring one is meaningless."""
    predictions.record(
        store,
        signal_id="vol",
        symbol="A",
        spot=100.0,
        view=view(direction="vol_only"),
        implied_move_pct=2.0,
        now=NOW,
    )
    predictions.resolve_due(store, FakeData(signed=-3.0), now=NOW + timedelta(hours=7))
    calib = predictions.calibration(store)
    assert calib.directional_n == 0 and calib.direction_accuracy is None
