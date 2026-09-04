"""Replay recorded sessions through the pipeline under a different config.

The question this exists to answer is "would this change have fired?", and
before it there was no way to ask. Live sessions produce a handful of trades a
day, so judging a threshold by trading it takes weeks and confounds the change
with the market. Replay separates them: same recorded inputs, different config,
see what moves.

Two recorded artefacts make it possible:

  * the audit log's `analyst_view` records — the model's ACTUAL output for a
    story, replayed rather than re-requested. That keeps a run free,
    deterministic and repeatable, and it isolates the variable under test: the
    config, not the model's mood.
  * `chains/*.jsonl` — the option quotes each decision was actually made
    against, captured from 3 Sep.

What it replays: the edge test, structure choice, strike selection and the
liquidity view of the gate — everything downstream of the model and upstream
of the book.

What it does NOT replay, and why that matters when reading the output: the
portfolio-dependent gate checks (heat, delta band, concentration,
correlation, rate limits) and fill outcomes. A signal this harness calls
`tradable` is one that reached the gate with a valid structure — not one that
would necessarily have been approved, filled, or profitable. It measures the
funnel, not the P&L.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from glassbox.chain import (
    ContractQuote,
    NoSuitableStrikesError,
    atm_straddle_mid,
    build_structure,
    structure_liquidity,
)
from glassbox.signal.edge import evaluate_edge
from glassbox.structures import (
    ImplausiblePricingError,
    Right,
    UndefinedRiskError,
    max_loss_per_spread,
)

# A chain capture more than this far from the decision is a different market.
MAX_CHAIN_SKEW = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class Decision:
    """One replayed signal, and where it stopped."""

    ts: str
    symbol: str
    headline: str
    stage: str  # where it stopped: analyst | no_chain | edge | chain | liquidity | tradable
    detail: str
    verdict: str = ""
    ratio: float | None = None
    vrp: float | None = None
    structure: str = ""
    spread_pct: float | None = None
    open_interest: int | None = None
    max_loss: float | None = None


@dataclass
class ReplayReport:
    label: str
    decisions: list[Decision] = field(default_factory=list)

    @property
    def by_stage(self) -> dict[str, int]:
        return dict(Counter(d.stage for d in self.decisions).most_common())

    @property
    def tradable(self) -> list[Decision]:
        return [d for d in self.decisions if d.stage == "tradable"]

    def summary(self) -> str:
        lines = [f"{self.label}: {len(self.decisions)} analyst views replayed"]
        for stage, n in self.by_stage.items():
            lines.append(f"  {n:5d}  {stage}")
        if self.tradable:
            lines.append(f"  --- {len(self.tradable)} reached the gate with a structure ---")
            for d in self.tradable[:20]:
                lines.append(
                    f"    {d.ts[11:19]} {d.symbol:6s} {d.verdict:15s} "
                    f"ratio={d.ratio:.2f} spread={d.spread_pct:.1f}% oi={d.open_interest} "
                    f"risk=${d.max_loss:,.0f}"
                )
        return "\n".join(lines)


def _load_chains(chains_dir: Path, day: date) -> dict[str, list[tuple[datetime, dict]]]:
    """Captured chains for one day, indexed by symbol and ordered by time."""
    path = Path(chains_dir) / f"{day:%Y-%m-%d}-chains.jsonl"
    out: dict[str, list[tuple[datetime, dict]]] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            out.setdefault(rec["symbol"], []).append((datetime.fromisoformat(rec["ts"]), rec))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue  # a truncated tail is normal on a file still being written
    for rows in out.values():
        rows.sort(key=lambda r: r[0])
    return out


def _nearest_chain(chains: dict, symbol: str, when: datetime) -> dict | None:
    rows = chains.get(symbol)
    if not rows:
        return None
    ts, rec = min(rows, key=lambda r: abs(r[0] - when))
    return rec if abs(ts - when) <= MAX_CHAIN_SKEW else None


def _quotes(rec: dict) -> list[ContractQuote]:
    expiry = date.fromisoformat(rec["expiry"])
    out = []
    for q in rec["quotes"]:
        try:
            out.append(
                ContractQuote(
                    symbol=q["symbol"],
                    right=Right(q["right"]),
                    strike=float(q["strike"]),
                    expiry=expiry,
                    bid=float(q["bid"]),
                    ask=float(q["ask"]),
                    open_interest=int(q.get("oi") or 0),
                    implied_volatility=q.get("iv"),
                    delta=q.get("delta"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # one malformed quote must not lose the whole chain
    return out


def _analyst_views(audit_dir: Path, day: date) -> list[dict]:
    path = Path(audit_dir) / f"{day:%Y-%m-%d}-trader.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") == "analyst_view":
            out.append(rec)
    return out


def replay_day(audit_dir, chains_dir, day: date, cfg, label: str = "") -> ReplayReport:
    """Re-run one recorded session's signals through the pipeline under `cfg`."""
    report = ReplayReport(label=label or f"{day:%Y-%m-%d}")
    chains = _load_chains(Path(chains_dir), day)

    for rec in _analyst_views(Path(audit_dir), day):
        symbol = rec.get("symbol") or ""
        ts = rec.get("ts", "")
        head = (rec.get("headline") or "")[:80]

        def stop(stage: str, detail: str, _ts=ts, _sym=symbol, _head=head, **kw) -> None:
            # loop variables bound as defaults: the closure outlives the iteration
            report.decisions.append(
                Decision(ts=_ts, symbol=_sym, headline=_head, stage=stage, detail=detail, **kw)
            )

        confidence = float(rec.get("confidence") or 0.0)
        if confidence < cfg.signal.min_confidence:
            stop("analyst", f"confidence {confidence:.2f} < {cfg.signal.min_confidence}")
            continue

        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            stop("no_chain", "unparseable timestamp")
            continue

        chain_rec = _nearest_chain(chains, symbol, when)
        if chain_rec is None:
            stop("no_chain", "no captured chain within 10 minutes of the decision")
            continue

        quotes = _quotes(chain_rec)
        spot = float(chain_rec["spot"])
        expiry = date.fromisoformat(chain_rec["expiry"])
        hours_to_expiry = max(
            0.0, (datetime.combine(expiry, when.timetz()) - when).total_seconds() / 3600
        )
        try:
            straddle = atm_straddle_mid(quotes, spot)
        except NoSuitableStrikesError as e:
            stop("chain", f"straddle unavailable: {e}")
            continue

        edge = evaluate_edge(
            expected_move_pct=float(rec.get("expected_move_pct") or 0.0),
            direction=rec.get("direction") or "",
            confidence=confidence,
            straddle_mid=straddle,
            spot=spot,
            hours_to_expiry=hours_to_expiry,
            horizon_hours=float(rec.get("horizon_hours") or 0.0),
            cfg=cfg,
        )
        verdict = str(edge.verdict)
        ratio = edge.ratio
        vrp = edge.vrp_ratio
        if not edge.tradable or not edge.eligible_structures:
            stop("edge", edge.detail, verdict=verdict, ratio=ratio, vrp=vrp)
            continue

        kind = edge.eligible_structures[0]
        try:
            structure, net = build_structure(
                kind, quotes, spot, float(rec.get("expected_move_pct") or 0.0), symbol, cfg
            )
            risk = max_loss_per_spread(structure, net)
        except (NoSuitableStrikesError, UndefinedRiskError, ImplausiblePricingError) as e:
            stop("chain", f"{type(e).__name__}: {e}", verdict=verdict, ratio=ratio, vrp=vrp)
            continue

        spread_pct, oi = structure_liquidity(structure, quotes)
        if spread_pct > cfg.gate.max_spread_pct_of_mid or oi < cfg.gate.min_open_interest:
            stop("liquidity", f"spread {spread_pct:.1f}% / OI {oi}", verdict=verdict, ratio=ratio,
                 vrp=vrp, spread_pct=spread_pct, open_interest=oi)
            continue

        stop(
            "tradable",
            f"{kind} at {net:+.2f}",
            verdict=verdict, ratio=ratio, vrp=vrp,
            structure=str(kind), spread_pct=spread_pct, open_interest=oi, max_loss=risk,
        )
    return report


def compare(base: ReplayReport, variant: ReplayReport) -> str:
    """Two runs over the same recording, side by side."""
    stages = sorted(set(base.by_stage) | set(variant.by_stage))
    width = max(len(s) for s in stages) + 2
    lines = [
        f"{'stage'.ljust(width)}{base.label:>12}{variant.label:>12}{'delta':>8}",
        "-" * (width + 32),
    ]
    for s in stages:
        b, v = base.by_stage.get(s, 0), variant.by_stage.get(s, 0)
        mark = f"{v - b:+d}" if v != b else "·"
        lines.append(f"{s.ljust(width)}{b:>12}{v:>12}{mark:>8}")
    return "\n".join(lines)


def _cli() -> int:
    """Replay recorded sessions, optionally against a modified config.

    Overrides are dotted paths into the config, so a threshold can be swept
    without editing the file the live system reads:

        uv run python -m glassbox.replay --days 2026-09-03 2026-09-04 \\
            --set signal.vrp_min_for_credit=0.90
    """
    import argparse

    from glassbox.config import load_config

    ap = argparse.ArgumentParser(description="Replay recorded sessions under a config")
    ap.add_argument("--audit-dir", default="audit")
    ap.add_argument("--chains-dir", default="chains")
    ap.add_argument("--days", nargs="+", required=True, help="YYYY-MM-DD, one or more")
    ap.add_argument("--set", dest="overrides", nargs="*", default=[],
                    help="section.key=value — compares against the unmodified config")
    ap.add_argument("--verbose", action="store_true", help="list every tradable signal")
    args = ap.parse_args()

    cfg = load_config()
    variant = cfg
    for override in args.overrides:
        try:
            path, raw = override.split("=", 1)
            section, key = path.split(".", 1)
        except ValueError:
            print(f"bad override {override!r}; expected section.key=value")
            return 2
        current = getattr(getattr(variant, section), key)
        value = type(current)(raw) if not isinstance(current, bool) else raw.lower() == "true"
        variant = variant.model_copy(update={
            section: getattr(variant, section).model_copy(update={key: value})
        })

    for raw_day in args.days:
        day = date.fromisoformat(raw_day)
        base = replay_day(args.audit_dir, args.chains_dir, day, cfg, label="recorded")
        if not base.decisions:
            print(f"{day}: nothing to replay — no analyst views recorded")
            continue
        if args.overrides:
            var = replay_day(args.audit_dir, args.chains_dir, day, variant, label="variant")
            print(f"\n=== {day} — {', '.join(args.overrides)} ===")
            print(compare(base, var))
            if args.verbose:
                print("\n" + var.summary())
        else:
            print(f"\n=== {day} ===")
            print(base.summary() if args.verbose else
                  "\n".join(f"  {n:5d}  {s}" for s, n in base.by_stage.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
