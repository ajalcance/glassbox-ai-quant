"""Reads the audit log into the shapes the dashboard renders.

The audit log is the source of truth for everything shown here. Nothing is
recomputed: if a number appears on the dashboard it is because the trader wrote
it at decision time, which is what makes the page evidence rather than a
re-enactment.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Stages in pipeline order. Used to render how far each signal travelled.
STAGES = ("filter", "analyst", "market_data", "edge", "chain", "sizing", "gate", "executed")


def read_records(audit_dir: str | Path, days: int = 3) -> list[dict]:
    """Most recent records, oldest first. Malformed lines are skipped, not
    fatal — a dashboard must never be the reason you cannot see the system.

    Each process writes its own day-file (YYYY-MM-DD-<role>.jsonl), so a day
    spans several files: group by the date prefix, take the last `days` days,
    then merge all roles' records into one timeline ordered by timestamp."""
    audit_dir = Path(audit_dir)
    if not audit_dir.exists():
        return []
    by_day: dict[str, list[Path]] = defaultdict(list)
    for path in audit_dir.glob("*.jsonl"):
        by_day[path.stem[:10]].append(path)
    records = []
    for day in sorted(by_day)[-days:]:
        for path in sorted(by_day[day]):
            for line in path.read_text(errors="replace").splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    records.sort(key=lambda r: r.get("ts", ""))
    return records


@dataclass
class Decision:
    """One news item's journey through the pipeline."""

    signal_id: str
    symbol: str = ""
    headline: str = ""
    ts: str = ""
    analyst: dict | None = None
    edge: dict | None = None
    ml: dict | None = None
    gate: dict | None = None
    order: dict | None = None
    dropped: dict | None = None

    @property
    def outcome(self) -> str:
        if self.order:
            return "traded"
        if self.gate and not self.gate.get("approved", True):
            return "vetoed"
        if self.dropped:
            return "dropped"
        if self.edge and not self.edge.get("verdict", "").startswith("no_edge") is False:
            return "no_edge"
        return "open"

    @property
    def reason(self) -> str:
        if self.order:
            return f"{self.order.get('structure', '')} x{self.order.get('qty', '')}"
        if self.gate and not self.gate.get("approved", True):
            failed = [c for c in self.gate.get("checks", []) if not c["passed"]]
            return "; ".join(f"{c['name']}: {c['detail']}" for c in failed)
        if self.dropped:
            return self.dropped.get("reason", "")
        if self.edge:
            return self.edge.get("detail", "")
        return ""

    def as_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "headline": self.headline,
            "ts": self.ts,
            "outcome": self.outcome,
            "reason": self.reason,
            "analyst": self.analyst,
            "edge": self.edge,
            "ml": self.ml,
            "gate": self.gate,
            "order": self.order,
        }


def build_decisions(records: list[dict]) -> list[Decision]:
    """Correlate records by signal_id into one row per news item."""
    by_id: dict[str, Decision] = {}

    def get(signal_id: str) -> Decision:
        if signal_id not in by_id:
            by_id[signal_id] = Decision(signal_id=signal_id)
        return by_id[signal_id]

    for r in records:
        kind = r.get("kind")
        sid = r.get("signal_id")

        if kind == "analyst_view" and sid:
            d = get(sid)
            d.symbol, d.headline, d.ts = r.get("symbol", ""), r.get("headline", ""), r["ts"]
            d.analyst = r
        elif kind == "edge_test" and sid:
            get(sid).edge = r
        elif kind == "ml" and sid:
            get(sid).ml = r
        elif kind == "gate" and sid:
            get(sid).gate = r
        elif kind in ("order_submit", "dry_run_order"):
            pid = r.get("position_id", "")
            sid2 = pid.replace("pos-", "") if pid.startswith("pos-") else sid
            if sid2:
                d = get(sid2)
                d.order = r
                d.ts = d.ts or r["ts"]
        elif kind == "signal_dropped":
            # Dropped events may predate a signal_id (filter stage), so key on
            # the news id when that is all we have.
            key = r.get("signal_id") or f"{r.get('symbol', '?')}-{r.get('news_id', '?')}"
            d = get(key)
            d.symbol = d.symbol or r.get("symbol", "")
            d.headline = d.headline or r.get("headline", "")
            d.ts = d.ts or r["ts"]
            d.dropped = r

    return sorted(by_id.values(), key=lambda d: d.ts, reverse=True)


@dataclass
class VetoSummary:
    by_check: Counter = field(default_factory=Counter)
    by_stage: Counter = field(default_factory=Counter)
    total_seen: int = 0
    total_traded: int = 0

    def as_dict(self) -> dict:
        return {
            "by_check": self.by_check.most_common(),
            "by_stage": self.by_stage.most_common(),
            "total_seen": self.total_seen,
            "total_traded": self.total_traded,
        }


def summarise_vetoes(records: list[dict]) -> VetoSummary:
    """What the system refused, and which rule refused it.

    This has no equivalent on the broker's side: an order that was never
    submitted leaves no trace at Alpaca. It only exists because we logged the
    decision not to place it.
    """
    s = VetoSummary()
    for r in records:
        kind = r.get("kind")
        if kind == "signal_dropped":
            s.total_seen += 1
            s.by_stage[r.get("stage", "unknown")] += 1
        elif kind == "gate":
            s.total_seen += 1
            if r.get("approved"):
                s.total_traded += 1
            else:
                s.by_stage["gate"] += 1
                for check in r.get("checks", []):
                    if not check["passed"]:
                        s.by_check[check["name"]] += 1
    return s


def pnl_by_arm(store) -> list[dict]:
    """Realised P&L attribution by structure — derived, not a copy of the
    broker's blotter."""
    totals: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for row in store.training_rows():
        entry = totals[row["kind"]]
        entry["trades"] += 1
        entry["wins"] += int(row["meta_label"] or 0)
        entry["pnl"] += float(row["realized_pnl"] or 0.0)
    return [
        {"arm": arm, **stats, "win_rate": stats["wins"] / stats["trades"]}
        for arm, stats in sorted(totals.items())
        if stats["trades"]
    ]


def audit_chain_status(audit_dir: str | Path) -> dict:
    """Verify today's hash chains — the page should show whether its own
    evidence is intact. One chain per writing process, each checked alone."""
    from glassbox.audit import verify_day

    ok, n, broken = verify_day(audit_dir)
    if n == 0 and ok:
        return {"verified": True, "records": 0, "note": "no records yet today"}
    return {
        "verified": ok,
        "records": n,
        "note": "" if ok else "CHAIN BROKEN: " + ", ".join(broken),
    }
