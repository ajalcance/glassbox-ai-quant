"""Report the observed distribution of edge-test inputs.

The thresholds in `config/default.yaml` — 1.3 for debits, 0.7 for credits, 1.35
and 0.90 for the volatility premium — are reasoned defaults, not fitted values.
Nothing in three trading days will make them statistically fitted either.

What this does is narrow the gap between "chosen" and "chosen with evidence":
after a dry-run session it shows where the ratios actually landed, so the
thresholds can be set against observed data rather than intuition. If every
signal scored 1.1, a debit threshold of 1.3 means the system never trades; if
they cluster at 3.0, it trades constantly and the threshold does nothing.

    uv run python -m glassbox.calibrate
"""

from __future__ import annotations

import argparse
import statistics
import sys

from glassbox.config import load_config
from glassbox.dashboard.audit_reader import read_records


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(p / 100 * len(ordered)))
    return ordered[idx]


def _histogram(values: list[float], edges: list[float]) -> list[tuple[str, int]]:
    buckets = []
    for lo, hi in zip([float("-inf"), *edges], [*edges, float("inf")], strict=True):
        n = sum(1 for v in values if lo <= v < hi)
        label = (
            f"< {hi:g}"
            if lo == float("-inf")
            else f">= {lo:g}"
            if hi == float("inf")
            else f"{lo:g}–{hi:g}"
        )
        buckets.append((label, n))
    return buckets


def _report(name: str, values: list[float], edges: list[float], thresholds: dict) -> None:
    print(f"\n{name}  (n={len(values)})")
    if not values:
        print("  no observations yet — run a session first")
        return
    print(
        f"  min {min(values):.2f}  p25 {_percentile(values, 25):.2f}  "
        f"median {statistics.median(values):.2f}  p75 {_percentile(values, 75):.2f}  "
        f"max {max(values):.2f}"
    )
    width = max(1, max(n for _, n in _histogram(values, edges)))
    for label, n in _histogram(values, edges):
        bar = "#" * int(28 * n / width)
        print(f"    {label:>10}  {n:>4}  {bar}")
    for label, value in thresholds.items():
        above = sum(1 for v in values if v >= value)
        print(
            f"  at {label} = {value}: {above}/{len(values)} "
            f"({100 * above / len(values):.0f}%) would be above"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Edge-test calibration report")
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args()

    cfg = load_config()
    records = [
        r for r in read_records(cfg.paths.audit_dir, days=args.days) if r.get("kind") == "edge_test"
    ]
    if not records:
        print("No edge_test records found. Run the trader through a session first:")
        print("  uv run python -m glassbox.runner --dry-run")
        return 1

    print(f"Edge-test calibration — {len(records)} evaluations over {args.days} day(s)")

    ratios = [float(r["ratio"]) for r in records if r.get("ratio")]
    _report(
        "expected / implied",
        ratios,
        [0.5, 0.7, 0.9, 1.1, 1.3, 1.6, 2.0, 3.0],
        {
            "edge_ratio_debit": cfg.signal.edge_ratio_debit,
            "edge_ratio_credit": cfg.signal.edge_ratio_credit,
        },
    )

    vrps = [float(r["vrp_ratio"]) for r in records if r.get("vrp_ratio")]
    _report(
        "implied / forecast realised (VRP)",
        vrps,
        [0.7, 0.9, 1.1, 1.35, 1.7, 2.5],
        {
            "vrp_max_for_debit": cfg.signal.vrp_max_for_debit,
            "vrp_min_for_credit": cfg.signal.vrp_min_for_credit,
        },
    )

    realized = [float(r["realized_move_pct"]) for r in records if r.get("realized_move_pct")]
    if realized:
        _report("move already spent before we saw it (%)", realized, [0.25, 0.5, 1.0, 2.0, 4.0], {})

    verdicts: dict[str, int] = {}
    for r in records:
        verdicts[r.get("verdict", "?")] = verdicts.get(r.get("verdict", "?"), 0) + 1
    print("\nverdicts")
    for verdict, n in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        print(f"  {verdict:<20} {n:>4}  ({100 * n / len(records):.0f}%)")

    tradable = sum(n for v, n in verdicts.items() if not v.endswith("no_edge"))
    print(
        f"\n{tradable}/{len(records)} evaluations produced a tradable verdict. "
        "If that is 0% the thresholds are too tight; if it is most of them, they "
        "are not filtering."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
