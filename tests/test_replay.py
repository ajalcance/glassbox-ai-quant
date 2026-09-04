"""Replay harness tests — the thing that lets a config change be measured
against recorded sessions instead of argued about."""

import json
from datetime import UTC, date, datetime

from glassbox.config import load_config
from glassbox.replay import compare, replay_day

CFG = load_config()
DAY = date(2026, 9, 4)
WHEN = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)


def write_recording(tmp_path, *, confidence=0.85, expected=4.0, oi=5000, spread=0.02):
    audit, chains = tmp_path / "audit", tmp_path / "chains"
    audit.mkdir(); chains.mkdir()
    (audit / f"{DAY}-trader.jsonl").write_text(json.dumps({
        "kind": "analyst_view", "ts": WHEN.isoformat(), "symbol": "AAPL",
        "headline": "Apple beats and raises", "confidence": confidence, "direction": "up",
        "expected_move_pct": expected, "horizon_hours": 48.0, "materiality": 0.9,
    }) + "\n")

    spot, quotes = 230.0, []
    for strike in range(190, 275, 5):
        for right in ("call", "put"):
            intrinsic = max(0.0, (spot - strike) if right == "call" else (strike - spot))
            mid = intrinsic + max(0.05, 3.0 - 0.08 * abs(strike - spot))
            quotes.append({
                "symbol": f"AAPL260918{right[0].upper()}{int(strike * 1000):08d}",
                "right": right, "strike": float(strike),
                "bid": mid - spread, "ask": mid + spread, "oi": oi, "iv": 0.3, "delta": 0.4,
            })
    (chains / f"{DAY}-chains.jsonl").write_text(json.dumps({
        "ts": WHEN.isoformat(), "symbol": "AAPL", "expiry": "2026-09-18",
        "spot": spot, "quotes": quotes,
    }) + "\n")
    return audit, chains


def test_a_strong_signal_replays_all_the_way_to_a_structure(tmp_path):
    audit, chains = write_recording(tmp_path)
    r = replay_day(audit, chains, DAY, CFG)
    assert len(r.decisions) == 1
    d = r.decisions[0]
    assert d.stage == "tradable", f"{d.stage}: {d.detail}"
    assert d.structure and d.max_loss > 0 and d.ratio > 1.0


def test_the_confidence_floor_is_applied_from_config_not_the_recording(tmp_path):
    """The recorded view is the model's output; every threshold comes from the
    config under test. That separation is the whole point."""
    audit, chains = write_recording(tmp_path, confidence=0.50)
    assert replay_day(audit, chains, DAY, CFG).by_stage == {"analyst": 1}


def test_an_illiquid_chain_stops_at_liquidity(tmp_path):
    audit, chains = write_recording(tmp_path, oi=5)
    d = replay_day(audit, chains, DAY, CFG).decisions[0]
    assert d.stage == "liquidity" and d.open_interest < CFG.gate.min_open_interest


def test_a_signal_with_no_captured_chain_is_reported_not_dropped(tmp_path):
    """Sessions before chain capture existed replay as no_chain rather than
    silently vanishing — an unmeasurable signal must not read as a refusal."""
    audit, chains = write_recording(tmp_path)
    (chains / f"{DAY}-chains.jsonl").unlink()
    assert replay_day(audit, chains, DAY, CFG).by_stage == {"no_chain": 1}


def test_compare_shows_what_a_config_change_moves(tmp_path):
    """The harness exists to answer "would this change have fired?" — here in
    the direction that matters most: what does tightening a band cost?"""
    audit, chains = write_recording(tmp_path, expected=4.0)
    base = replay_day(audit, chains, DAY, CFG, label="base")
    assert base.by_stage.get("tradable", 0) == 1

    tightened = CFG.model_copy(update={
        "signal": CFG.signal.model_copy(update={"edge_ratio_debit": 2.0})
    })
    variant = replay_day(audit, chains, DAY, tightened, label="tight")
    assert variant.by_stage.get("tradable", 0) == 0, "the tightened band must refuse it"
    assert variant.by_stage.get("edge", 0) == 1

    table = compare(base, variant)
    assert "tradable" in table and "-1" in table
