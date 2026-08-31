#!/usr/bin/env python3
"""Measure how much the indicative options feed inflates quoted spreads.

    uv run python soak/spread_calibration.py

The liquidity gate compares `gate.max_spread_pct_of_mid` against quotes from
Alpaca's free *indicative* feed, which is systematically wider than the OPRA
consolidated book — so the cap filters more than its number says. Rather than
paying for OPRA or guessing a correction, this measures the inflation from two
sources the system already has:

  indicative side: the gate audit log records the spread the liquidity check
                   measured for every candidate, passes included — the exact
                   number the system acted on, at the moment it acted;
  real side:       historical option TRADES are genuine OPRA prints (the free
                   plan's 15-minute delay is irrelevant for yesterday), and
                   where trades execute bounds the real spread from inside.

Two effective-spread estimators per contract, both quote-free:
  minute-range: median over minutes with >=2 prints of (high-low)/mean —
                the bid-ask bounce shows up as intra-minute price range;
  Roll (1984):  2*sqrt(-cov(dp_t, dp_t-1))/mean price — the classic
                estimator of effective spread from trade prices alone.

Effective spread is a floor on real spread (prints often land at the touch,
frequently inside), so the derived inflation is an upper bound — one reason
the recalibrated cap still carries a hard ceiling, and why genuinely thin
contracts (few prints) are reported but excluded from the pooled factor.

Result from the 31 Aug session, which set `max_spread_pct_of_mid: 20.0`:
contracts printing 200-1,475 times at 1.4-3.0% effective were quoted at
15.0-17.3% (5-12x inflation on single names); SPY read ~1.3-2x; XLK condor
legs with 13-25 prints/day were quoted 34.7% and stay excluded at any cap.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glassbox.config import load_config
from glassbox.data.alpaca_client import option_data_client

MIN_PRINTS = 50  # below this a contract is "thin": reported, not pooled


def occ_symbols(structure: str) -> list[str]:
    """OCC leg symbols from an audit structure key like
    'ADBE|bull_put_spread|20260904|longp282.5x1:shortp285x1'."""
    try:
        underlying, _kind, expiry, legs = structure.split("|", 3)
    except ValueError:
        return []
    out = []
    for leg in legs.split(":"):
        m = re.match(r"(?:long|short)([cp])([0-9.]+)x\d+", leg)
        if not m:
            continue
        right, strike = m.group(1).upper(), float(m.group(2))
        out.append(f"{underlying}{expiry[2:]}{right}{round(strike * 1000):08d}")
    return out


def gate_liquidity_readings(audit_dir: Path, day: str) -> dict[str, float]:
    """structure -> indicative spread % the gate measured, from the audit log."""
    readings: dict[str, float] = {}
    for f in sorted(glob.glob(str(audit_dir / "*.jsonl"))):
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("kind") != "gate" or not r.get("ts", "").startswith(day):
                    continue
                for c in r.get("checks", []):
                    if c["name"] == "liquidity":
                        m = re.search(r"spread ([0-9.]+)%", c["detail"])
                        if m:
                            readings[r.get("structure", "")] = float(m.group(1))
    return readings


def effective_spreads(client, symbol: str, day: datetime) -> tuple[int, float, float]:
    """(print count, minute-range %, Roll %) from one session's OPRA prints."""
    from alpaca.data.requests import OptionTradesRequest

    trades = client.get_option_trades(
        OptionTradesRequest(
            symbol_or_symbols=symbol,
            start=day.replace(hour=13, minute=30),
            end=day.replace(hour=20, minute=0),
            limit=5000,
        )
    ).data.get(symbol, [])
    if len(trades) < 4:
        return len(trades), float("nan"), float("nan")
    per_min: dict[str, list[float]] = {}
    for t in trades:
        per_min.setdefault(t.timestamp.strftime("%H:%M"), []).append(float(t.price))
    ranges = [
        100 * (max(v) - min(v)) / statistics.mean(v)
        for v in per_min.values()
        if len(v) >= 2 and statistics.mean(v) > 0
    ]
    minute_range = statistics.median(ranges) if ranges else float("nan")
    px = [float(t.price) for t in trades]
    from itertools import pairwise

    diffs = [b - a for a, b in pairwise(px)]
    cov = statistics.covariance(diffs[:-1], diffs[1:]) if len(diffs) >= 3 else 0.0
    mean_px = statistics.mean(px)
    roll = 100 * 2 * ((-cov) ** 0.5) / mean_px if cov < 0 and mean_px > 0 else float("nan")
    return len(trades), minute_range, roll


def main() -> int:
    parser = argparse.ArgumentParser(description="indicative-feed spread calibration")
    parser.add_argument("--day", default=None, help="session day YYYY-MM-DD (default: latest with gate records)")
    parser.add_argument("--audit-dir", default=str(ROOT / "audit"))
    args = parser.parse_args()

    cfg = load_config()
    audit_dir = Path(args.audit_dir)
    if args.day:
        day_str = args.day
    else:
        days = sorted(
            {p.name[:10] for p in audit_dir.glob("*.jsonl")}, reverse=True
        )
        day_str = days[0] if days else ""
    readings = gate_liquidity_readings(audit_dir, day_str)
    if not readings:
        print(f"no gate liquidity readings in {audit_dir} for {day_str!r} — nothing to calibrate against")
        return 1

    day = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=UTC)
    client = option_data_client()
    cap = cfg.gate.max_spread_pct_of_mid
    print(f"SPREAD CALIBRATION — session {day_str}, cap {cap}% (indicative units)")
    print(f"{'contract':24} {'prints':>6} {'range%':>7} {'roll%':>6} {'indicative%':>12} {'inflation':>9}")

    factors = []
    for structure, indicative in sorted(readings.items()):
        for sym in occ_symbols(structure):
            try:
                n, rng, roll = effective_spreads(client, sym, day)
            except Exception as e:  # noqa: BLE001 -- survey, not pipeline
                print(f"{sym:24} error: {type(e).__name__}: {e}")
                continue
            estimates = [x for x in (rng, roll) if not math.isnan(x)]
            eff = min(estimates) if estimates else float("nan")
            thin = n < MIN_PRINTS
            infl = indicative / eff if not math.isnan(eff) and eff > 0 else float("nan")
            tag = "  (thin — excluded)" if thin else ""
            print(f"{sym:24} {n:>6} {rng:>7.1f} {roll:>6.1f} {indicative:>12.1f} "
                  + (f"{infl:>8.1f}x{tag}" if not math.isnan(infl) else "        —" + tag))
            if not thin and not math.isnan(infl):
                factors.append(infl)

    if not factors:
        print("\nno print-rich contracts to pool — leave the cap unchanged")
        return 1
    print(f"\npooled inflation (print-rich contracts): "
          f"{', '.join(f'{x:.1f}x' for x in sorted(factors))}  ->  median {statistics.median(factors):.2f}x")
    print(f"current cap {cap}% in indicative units is roughly "
          f"{cap / statistics.median(factors):.1f}% of real spread for print-rich names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
