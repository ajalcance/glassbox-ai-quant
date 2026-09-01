"""Regime and macro-blackout tests.

The invariants that matter: regime scales but never creates or fully vetoes,
unknown environment means multiplier 1.0 (absence of evidence is not evidence of
hostility), and near a release the short-premium trade is refused while long
convexity is only haircut.
"""

from datetime import UTC, datetime

import pytest

from glassbox.config import load_config
from glassbox.macro import current_window
from glassbox.regime import RegimeReading, compute, percentile_of_last, ratio_series
from glassbox.signal.edge import EdgeVerdict, vrp_permits

CFG = load_config()
ET = UTC  # events carry their own offsets; tests use explicit ISO


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# --- percentile machinery -------------------------------------------------


def test_percentile_of_last_is_self_referencing():
    rising = [float(i) for i in range(30)]
    assert percentile_of_last(rising) == pytest.approx(1.0)
    falling = list(reversed(rising))
    assert percentile_of_last(falling) == pytest.approx(0.0)
    assert percentile_of_last([5.0] * 15 + [5.0]) == pytest.approx(1.0)  # ties count below


def test_percentile_needs_history():
    assert percentile_of_last([1.0, 2.0]) is None


def test_ratio_series_aligns_tails():
    """Unequal histories align on their most recent overlap."""
    assert ratio_series([2.0, 4.0, 8.0], [1.0, 2.0]) == [4.0, 4.0]


# --- the reading ----------------------------------------------------------


def test_unknown_regime_multiplies_by_one():
    """Absence of evidence about the environment is not evidence it is hostile."""
    unknown = RegimeReading(None, {}, "regime unknown")
    assert unknown.size_multiplier(CFG) == 1.0
    assert unknown.vrp_shift(CFG) == 0.0


def test_calm_market_keeps_full_size_and_stress_hits_the_floor():
    calm = RegimeReading(0.0, {}, "calm")
    stressed = RegimeReading(1.0, {}, "stressed")
    assert calm.size_multiplier(CFG) == pytest.approx(1.0)
    assert stressed.size_multiplier(CFG) == pytest.approx(CFG.regime.min_size_multiplier)
    # Regime scales; it never fully vetoes.
    assert stressed.size_multiplier(CFG) > 0


def test_vrp_shift_is_centred_and_symmetric():
    assert RegimeReading(0.5, {}, "normal").vrp_shift(CFG) == pytest.approx(0.0)
    assert RegimeReading(1.0, {}, "s").vrp_shift(CFG) == pytest.approx(CFG.regime.max_vrp_shift)
    assert RegimeReading(0.0, {}, "c").vrp_shift(CFG) == pytest.approx(-CFG.regime.max_vrp_shift)


class StubData:
    """Deterministic daily closes: VXX rising against VXZ = stress."""

    def __init__(self, stressed: bool):
        self.stressed = stressed

    def daily_closes(self, symbol, days=60):
        n = 40
        if symbol in ("VXX", "LQD", "SPLV"):
            return [10.0 + (i * 0.1 if self.stressed else -i * 0.05) for i in range(n)]
        if symbol in ("VXZ", "HYG", "SPHB"):
            return [10.0] * n
        # universe members: falling in stress -> weak breadth
        return [100.0 - i if self.stressed else 100.0 + i for i in range(n)]


def test_compute_reads_stress_from_the_panel():
    stressed = compute(StubData(True), {f"S{i}" for i in range(15)}, CFG)
    calm = compute(StubData(False), {f"S{i}" for i in range(15)}, CFG)
    assert stressed.known and calm.known
    assert stressed.score > 0.8 and calm.score < 0.2
    assert "vol_term" in stressed.factors and "breadth" in stressed.factors


def test_missing_factors_drop_out_rather_than_defaulting():
    class Sparse:
        def daily_closes(self, symbol, days=60):
            raise ConnectionError("proxy unavailable")

    reading = compute(Sparse(), set(), CFG)
    assert not reading.known
    assert "unknown" in reading.detail


# --- macro windows ---------------------------------------------------------


def test_blackout_opens_before_and_closes_after():
    w = current_window(CFG, at("2026-09-01T07:59:00-04:00"))
    assert not w.active, "2h01m before the release is outside the window"
    assert current_window(CFG, at("2026-09-01T08:00:00-04:00")).active
    assert current_window(CFG, at("2026-09-01T10:30:00-04:00")).active
    assert not current_window(CFG, at("2026-09-01T10:31:00-04:00")).active


def test_quiet_monday_reports_the_next_event():
    w = current_window(CFG, at("2026-08-31T10:00:00-04:00"))
    assert not w.active
    assert "ISM" in w.detail


def test_nfp_lands_on_submission_morning():
    w = current_window(CFG, at("2026-09-04T08:00:00-04:00"))
    assert w.active and "Nonfarm" in w.event_name


# --- the interaction that motivated all of this ----------------------------


def test_gate_refuses_short_premium_into_a_release(bull_put):
    from glassbox.gate import evaluate
    from tests.test_gate import ctx, veto_names

    window = current_window(CFG, at("2026-09-01T09:00:00-04:00"))
    d = evaluate(ctx(structure=bull_put, macro_window=window), CFG)
    assert "macro_blackout" in veto_names(d), (
        "selling insurance right before the insured event is the trade the "
        "distortion manufactures — it must be refused"
    )


def test_gate_permits_long_convexity_into_a_release(store, audit):
    from glassbox.gate import evaluate
    from glassbox.structures import LegSide, Right, Structure, StructureKind
    from tests.conftest import EXP, leg
    from tests.test_gate import ctx, veto_names

    debit = Structure(
        StructureKind.CALL_DEBIT_SPREAD,
        "SPY",
        (
            leg("SPY260918C00450000", Right.CALL, 450, LegSide.LONG),
            leg("SPY260918C00455000", Right.CALL, 455, LegSide.SHORT),
        ),
    )
    window = current_window(CFG, at("2026-09-01T09:00:00-04:00"))
    d = evaluate(ctx(structure=debit, macro_window=window), CFG)
    assert "macro_blackout" not in veto_names(d)
    assert EXP  # silence unused-import pedantry


def test_stress_relaxes_the_credit_floor():
    """A VRP just under the floor is refused in a normal market but permitted
    in stress, where premium is genuinely richer. 0.65 sits between the floor
    (0.72) and the fully-stressed floor (0.72 - 0.15)."""
    normal = vrp_permits(EdgeVerdict.SHORT_PREMIUM, 0.65, CFG, vrp_shift=0.0)
    stressed = vrp_permits(EdgeVerdict.SHORT_PREMIUM, 0.65, CFG, vrp_shift=0.15)
    assert not normal[0] and stressed[0]


def test_stress_tightens_the_debit_ceiling():
    """VRP 1.25 buys fine in calm but is refused in stress."""
    normal = vrp_permits(EdgeVerdict.LONG_CONVEXITY, 1.25, CFG, vrp_shift=0.0)
    stressed = vrp_permits(EdgeVerdict.LONG_CONVEXITY, 1.25, CFG, vrp_shift=0.15)
    assert normal[0] and not stressed[0]


def test_context_multiplier_shrinks_but_cannot_inflate():
    from glassbox.sizing import size_position

    full = size_position(100_000, 100.0, 0.9, CFG, context_multiplier=1.0)
    half = size_position(100_000, 100.0, 0.9, CFG, context_multiplier=0.5)
    inflated = size_position(100_000, 100.0, 0.9, CFG, context_multiplier=5.0)
    assert half.qty <= full.qty
    assert inflated.qty == full.qty, "context can shrink conviction, never inflate it"
