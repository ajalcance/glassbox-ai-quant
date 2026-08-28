"""Nightly report.

Runs after the US close and turns a day of audit records into a written
account of what the system did, what it refused, and what it learned. The
numbers are computed deterministically from the log; only the narrative comes
from a model, and it is given the numbers rather than asked to find them.

The report feeds three consumers from one artifact: the dashboard, that day's
build-in-public post, and — on the final day — the first draft of the one-page
write-up the submission requires.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from glassbox.audit import day_files
from glassbox.clock import market_date


@dataclass
class DayStats:
    day: str
    news_seen: int = 0
    reached_analyst: int = 0
    tradable_edge: int = 0
    gate_evaluated: int = 0
    gate_approved: int = 0
    orders_submitted: int = 0
    positions_closed: int = 0
    realized_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    drops_by_stage: dict = field(default_factory=dict)
    vetoes_by_check: dict = field(default_factory=dict)
    exits_by_barrier: dict = field(default_factory=dict)
    halts: list = field(default_factory=list)
    errors: dict = field(default_factory=dict)
    equity: float | None = None

    @property
    def win_rate(self) -> float | None:
        total = self.wins + self.losses
        return self.wins / total if total else None

    @property
    def funnel(self) -> list[tuple[str, int]]:
        """Where the day's news actually went. Most of it should die early."""
        return [
            ("news seen", self.news_seen),
            ("reached the analyst", self.reached_analyst),
            ("had a tradable edge", self.tradable_edge),
            ("approved by the gate", self.gate_approved),
            ("orders placed", self.orders_submitted),
        ]


def collect(audit_dir: str | Path, day: date | None = None) -> DayStats:
    """Deterministic statistics for one day. No model involved."""
    day = day or market_date()
    paths = day_files(audit_dir, day)
    if not paths:
        return DayStats(day=day.isoformat())

    records = []
    for path in paths:
        for line in path.read_text(errors="replace").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    records.sort(key=lambda r: r.get("ts", ""))

    s = DayStats(day=day.isoformat())
    drops, vetoes, barriers, errors = Counter(), Counter(), Counter(), Counter()

    for r in records:
        kind = r.get("kind")
        if kind == "signal_dropped":
            s.news_seen += 1
            drops[r.get("stage", "unknown")] += 1
        elif kind == "analyst_view":
            s.news_seen += 1
            s.reached_analyst += 1
        elif kind == "edge_test":
            if not str(r.get("verdict", "")).startswith("no_edge"):
                s.tradable_edge += 1
        elif kind == "gate":
            s.gate_evaluated += 1
            if r.get("approved"):
                s.gate_approved += 1
            for check in r.get("checks", []):
                if not check.get("passed"):
                    vetoes[check["name"]] += 1
        elif kind in ("order_submit", "dry_run_order"):
            if not r.get("closing"):
                s.orders_submitted += 1
        elif kind == "manage" and r.get("action") == "close":
            s.positions_closed += 1
            barriers[r.get("barrier", "unknown")] += 1
            pnl = float(r.get("unrealized_pnl") or 0.0)
            s.realized_pnl += pnl
            if r.get("label") == 1:
                s.wins += 1
            else:
                s.losses += 1
        elif kind in ("halt", "guard_breach"):
            s.halts.append(r.get("reason", ""))
        elif kind.endswith("_error"):
            errors[kind] += 1

    s.drops_by_stage = dict(drops.most_common())
    s.vetoes_by_check = dict(vetoes.most_common())
    s.exits_by_barrier = dict(barriers.most_common())
    s.errors = dict(errors.most_common())
    return s


def analyst_calibration(store) -> dict | None:
    from glassbox import predictions

    calib = predictions.calibration(store)
    return calib.as_dict() if calib else None


def learning_state(cfg, store) -> dict:
    from glassbox.ml.bandit import ThompsonBandit
    from glassbox.ml.metalabel import MetaLabeler

    meta = MetaLabeler.load(
        Path(cfg.paths.models_dir) / "metalabel.pkl",
        min_samples=cfg.ml.min_training_samples,
    )
    return {
        "meta_labeler": {
            "trained": meta.is_trained,
            "n_samples": meta.n_samples,
            "min_samples": meta.min_samples,
        },
        "bandit": ThompsonBandit(store).summary(),
        "analyst_calibration": analyst_calibration(store),
    }


NARRATIVE_SYSTEM = """\
You are writing the daily operations note for an automated options trading \
system, for an audience of engineers and a brokerage's own API team.

Rules:
- Every number you cite is given to you below. Do not invent, estimate, or \
extrapolate any figure. If something is not in the data, do not mention it.
- Be plain and unsentimental. No hype, no "exciting", no exclamation marks.
- A day with no trades is a normal and often correct outcome for a system whose \
job includes refusing bad trades. Write it that way, not as a failure.
- Small sample sizes mean nothing. Never describe one or two outcomes as a \
trend, an edge, or evidence that a strategy works.
- Lead with what actually happened, then what the system refused and why, then \
anything that needs a human.
- 150-250 words, prose paragraphs, no bullet lists, no headings."""


def narrative(llm, cfg, stats: DayStats, learning: dict) -> str:
    """The written summary. Given the numbers; asked only to describe them."""
    payload = {
        "day": stats.day,
        "funnel": dict(stats.funnel),
        "positions_closed": stats.positions_closed,
        "realized_pnl": round(stats.realized_pnl, 2),
        "wins": stats.wins,
        "losses": stats.losses,
        "exits_by_barrier": stats.exits_by_barrier,
        "why_news_was_dropped": stats.drops_by_stage,
        "gate_vetoes_by_check": stats.vetoes_by_check,
        "halts": stats.halts,
        "errors": stats.errors,
        "meta_labeler": learning["meta_labeler"],
        "bandit_cells": len(learning["bandit"]),
    }
    return llm.extract_text(
        model=cfg.llm.report_model,
        system=NARRATIVE_SYSTEM,
        user="Write the operations note for this day.\n\n" + json.dumps(payload, indent=2),
        max_tokens=cfg.llm.report_max_tokens,
    )


def render_markdown(stats: DayStats, learning: dict, prose: str, cli_snapshot: dict | None) -> str:
    lines = [f"# GlassBox — {stats.day}", ""]
    if prose:
        lines += [prose.strip(), ""]

    lines += ["## What the day looked like", "", "| Stage | Count |", "|---|---:|"]
    lines += [f"| {name} | {n} |" for name, n in stats.funnel]
    lines.append("")

    if stats.positions_closed:
        wr = stats.win_rate
        lines += [
            "## Closed positions",
            "",
            f"- {stats.positions_closed} closed, {stats.wins} up / {stats.losses} down"
            + (f" ({wr:.0%})" if wr is not None else ""),
            f"- Realised P&L: ${stats.realized_pnl:+,.2f}",
            "- Exits by barrier: "
            + (", ".join(f"{k} {v}" for k, v in stats.exits_by_barrier.items()) or "none"),
            "",
            "_Too few outcomes to read as evidence of anything._",
            "",
        ]

    if stats.vetoes_by_check or stats.drops_by_stage:
        lines += ["## What was refused, and by what", ""]
        for stage, n in stats.drops_by_stage.items():
            lines.append(f"- dropped at `{stage}`: {n}")
        for check, n in stats.vetoes_by_check.items():
            lines.append(f"- gate veto `{check}`: {n}")
        lines.append("")

    m = learning["meta_labeler"]
    lines += [
        "## Learning state",
        "",
        "- Meta-labeler: "
        + (
            f"trained on {m['n_samples']} outcomes"
            if m["trained"]
            else f"abstaining ({m['n_samples']}/{m['min_samples']} outcomes) — "
            f"the analyst's own confidence is used until then"
        ),
        f"- Bandit: {len(learning['bandit'])} arm/regime cells with history",
    ]
    calib = learning.get("analyst_calibration")
    if calib:
        lines.append(
            f"- Analyst: {calib['n']} scored estimate(s), mean expected "
            f"{calib['mean_expected_pct']:.2f}% against {calib['mean_actual_pct']:.2f}% "
            f"actual (bias {calib['bias']:.2f}x)"
        )
        if calib["direction_accuracy"] is not None:
            lines.append(
                f"  - direction {calib['direction_accuracy']:.0%} correct over "
                f"{calib['directional_n']} directional calls"
            )
    for arm in learning["bandit"]:
        lines.append(
            f"  - `{arm['arm']}` in `{arm['regime']}`: {arm['pulls']} pulls, "
            f"posterior mean {arm['mean']:.2f}"
        )
    lines.append("")

    if cli_snapshot:
        lines += [
            "## Independent account check (Alpaca CLI)",
            "",
            "Fetched with the Alpaca CLI rather than the SDK, so the figures below",
            "come through a separate binary, code path and auth than the trader uses.",
            "",
            f"- Equity: ${cli_snapshot.get('equity', 'n/a')}",
            f"- Open positions: {cli_snapshot.get('position_count', 'n/a')}",
            "- Agreement with local state: "
            + ("yes" if cli_snapshot.get("agrees") else "NO — investigate"),
            "",
        ]

    if stats.halts:
        lines += ["## Halts", ""] + [f"- {h}" for h in stats.halts] + [""]
    if stats.errors:
        lines += ["## Errors", ""] + [f"- `{k}`: {v}" for k, v in stats.errors.items()] + [""]

    return "\n".join(lines)


def write_report(cfg, store, llm, day: date | None = None, cli_snapshot=None) -> Path:
    day = day or market_date()
    stats = collect(cfg.paths.audit_dir, day)
    learning = learning_state(cfg, store)

    prose = ""
    if llm is not None:
        try:
            prose = narrative(llm, cfg, stats, learning)
        except Exception as e:  # noqa: BLE001 -- the numbers are the report; the
            # narrative is a convenience and must never block it being written.
            prose = f"_Narrative unavailable ({type(e).__name__})._"

    # Under data/ so reports land on the persisted state volume in deployed
    # containers. The old bare reports/ sat in the container's own filesystem
    # layer, where every image recreate silently discarded them — the nightly
    # evidence trail must survive a redeploy.
    out_dir = Path("data/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day:%Y-%m-%d}.md"
    path.write_text(render_markdown(stats, learning, prose, cli_snapshot))
    (out_dir / f"{day:%Y-%m-%d}.json").write_text(
        json.dumps({"stats": asdict(stats), "learning": learning}, indent=2)
    )
    return path
