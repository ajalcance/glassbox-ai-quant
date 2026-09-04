"""Replay the barrier ladder against recorded P&L paths.

`replay.py` answers "would this signal have been entered?". This answers the
harder half — "would a different exit have done better?" — and it does so
against recorded truth rather than a simulated market.

Every management tick writes a `manage` audit record carrying the position's
unrealised P&L at that moment, marked at liquidation value. Those records are
a tick-by-tick path: 39 to 697 points per position across the first week. Given
the path and the position's metadata, a PositionView can be reconstructed at
every tick and put back through `evaluate_position` under any config. No fill
model, no synthetic quotes, no assumption about what the market "would have"
done — the marks are what the system actually saw.

THE ONE LIMIT, and it is not negotiable: the recorded path ends where the
position actually closed. A config that would have exited EARLIER is fully
answerable, because every tick up to that point really happened. A config that
would have held LONGER has no data, and this module reports that as
`no_exit_in_window` rather than extrapolating. Read a run accordingly: it can
prove a tighter exit was available, and can never prove a looser one was
better.

Two further honesties. Replayed P&L is a MARK, while the recorded outcome is a
FILL — TLT's last mark was +$68 against a +$85 realised fill, so small
differences between them are exit slippage, not error. And barriers needing
context the path does not carry — `thesis_complete` (spot), `thesis_broken`
(later news), `macro_risk`, `bell` — cannot be evaluated here; a position whose
real exit used one is flagged rather than silently re-decided.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from glassbox.manage import Action, Barrier, PositionView, evaluate_position
from glassbox.structures import StructureKind

# Barriers this module cannot evaluate: they need spot, later news, or session
# context that a P&L path does not carry.
CONTEXT_BARRIERS = {"thesis_complete", "thesis_broken", "macro_risk", "bell"}


@dataclass(frozen=True, slots=True)
class ExitReplay:
    position_id: str
    underlying: str
    kind: str
    qty: int
    entry_price: float
    ticks: int
    actual_barrier: str
    actual_realized: float
    peak_pnl: float
    trough_pnl: float
    final_mark: float
    replay_barrier: str  # "" when nothing fired inside the window
    replay_pnl: float | None
    replay_tick: int | None
    replay_ts: str

    @property
    def exited_in_window(self) -> bool:
        return self.replay_barrier != ""

    @property
    def actual_needs_context(self) -> bool:
        """The real exit used a barrier this module cannot evaluate."""
        return self.actual_barrier in CONTEXT_BARRIERS

    @property
    def delta(self) -> float | None:
        """Replayed mark minus what was actually realised. Positive = better.

        Compares a mark against a fill, so treat single-digit differences as
        exit slippage rather than signal.
        """
        return None if self.replay_pnl is None else self.replay_pnl - self.actual_realized


def _paths(audit_dir: Path, days) -> dict[str, list[dict]]:
    """Per position, its ordered manage ticks."""
    out: dict[str, list[dict]] = {}
    for day in days:
        path = Path(audit_dir) / f"{day:%Y-%m-%d}-trader.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "manage" and rec.get("unrealized_pnl") is not None:
                out.setdefault(rec["position_id"], []).append(rec)
    for rows in out.values():
        rows.sort(key=lambda r: r["ts"])
    return out


def _hours_to_expiry(row, when: datetime) -> float:
    try:
        legs = json.loads(row["legs_json"])
        expiry = date.fromisoformat(legs[0]["expiry"])
    except (KeyError, IndexError, ValueError, TypeError):
        return 720.0  # unknown expiry must not fire the expiry barrier
    close = datetime.combine(expiry, when.timetz())
    return max(0.0, (close - when).total_seconds() / 3600)


def replay_exits(store, audit_dir, days, cfg, deadline: datetime | None = None) -> list[ExitReplay]:
    """Re-run every closed position's recorded path through the barriers."""
    paths = _paths(Path(audit_dir), days)
    out: list[ExitReplay] = []

    for row in store._conn.execute(
        "SELECT * FROM positions WHERE realized_pnl IS NOT NULL ORDER BY opened_at"
    ):
        ticks = paths.get(row["position_id"], [])
        if not ticks:
            continue

        pnls = [float(t["unrealized_pnl"]) for t in ticks]
        opened_at = datetime.fromisoformat(row["opened_at"])
        qty = int(row["qty"])
        entry = float(row["entry_price"])
        max_loss_per_spread = float(row["max_loss"]) / qty if qty else float(row["max_loss"])

        fired_barrier, fired_pnl, fired_tick, fired_ts = "", None, None, ""
        peak = 0.0
        for i, tick in enumerate(ticks):
            pnl = float(tick["unrealized_pnl"])
            peak = max(peak, pnl)
            when = datetime.fromisoformat(tick["ts"])
            view = PositionView(
                position_id=row["position_id"],
                kind=StructureKind(row["kind"]),
                qty=qty,
                entry_price=entry,
                # the mark that produces exactly this recorded P&L
                current_price=entry + pnl / (100 * qty) if qty else entry,
                max_loss_per_spread=max_loss_per_spread,
                opened_at=opened_at,
                horizon_hours=float(row["horizon_hours"] or 0.0),
                hours_to_expiry=_hours_to_expiry(row, when),
                peak_pnl=peak,
                # deliberately left empty: a path carries no spot, so the
                # thesis barriers cannot and must not fire here.
                thesis_direction="",
                thesis_move_pct=0.0,
            )
            decision = evaluate_position(view, cfg, when, deadline)
            if decision.action is Action.CLOSE and decision.barrier is not Barrier.NONE:
                fired_barrier = str(decision.barrier)  # StrEnum: "stop", "trail", ...
                fired_pnl, fired_tick, fired_ts = pnl, i, tick["ts"]
                break

        out.append(ExitReplay(
            position_id=row["position_id"],
            underlying=row["underlying"],
            kind=row["kind"],
            qty=qty,
            entry_price=entry,
            ticks=len(ticks),
            actual_barrier=str(row["exit_barrier"] or ""),
            actual_realized=float(row["realized_pnl"]),
            peak_pnl=max(pnls),
            trough_pnl=min(pnls),
            final_mark=pnls[-1],
            replay_barrier=fired_barrier,
            replay_pnl=fired_pnl,
            replay_tick=fired_tick,
            replay_ts=fired_ts,
        ))
    return out


def render(results: list[ExitReplay], label: str = "") -> str:
    """A table, plus the totals that are actually comparable."""
    if not results:
        return "no closed positions with recorded paths"
    lines = [
        f"{label or 'exit replay'} — {len(results)} closed positions with recorded paths",
        "",
        (
            f"{'position':22s}{'ticks':>7}{'peak':>9}{'trough':>9}"
            f"{'actual':>10}{'  ':2}{'replayed':>10}  barrier"
        ),
        "-" * 88,
    ]
    comparable_actual = comparable_replay = 0.0
    n_comparable = 0
    for r in sorted(results, key=lambda x: x.position_id):
        if r.exited_in_window:
            replayed = f"{r.replay_pnl:+,.0f}"
            barrier = f"{r.replay_barrier} @tick {r.replay_tick}"
            if not r.actual_needs_context:
                comparable_actual += r.actual_realized
                comparable_replay += r.replay_pnl
                n_comparable += 1
        else:
            replayed, barrier = "—", "no exit in window"
        flag = " *" if r.actual_needs_context else ""
        lines.append(
            f"{r.underlying + ' ' + r.position_id[-8:]:22s}{r.ticks:>7}"
            f"{r.peak_pnl:>+9.0f}{r.trough_pnl:>+9.0f}"
            f"{r.actual_realized:>+10.0f}{'  ':2}{replayed:>10}  {barrier}{flag}"
        )
    lines += [
        "-" * 88,
        (
            f"comparable subset ({n_comparable} positions): "
            f"actual {comparable_actual:+,.0f}  →  replayed {comparable_replay:+,.0f}  "
            f"({comparable_replay - comparable_actual:+,.0f})"
        ),
        "",
        "* real exit used a barrier this replay cannot evaluate (needs spot, later",
        "  news or session context) — excluded from the comparable total.",
        "Replayed figures are MARKS; actuals are FILLS. Differences of a few dollars",
        "are exit slippage. A config that would have held LONGER than the recording",
        "shows 'no exit in window' — the path simply ends there.",
    ]
    return "\n".join(lines)


def _cli() -> int:
    """Sweep an exit parameter across the recorded paths.

        uv run python -m glassbox.replay_exits --db data/glassbox.db \
            --set manage.trail_keep_pct=70
    """
    import argparse

    from glassbox.config import load_config
    from glassbox.store import Store

    ap = argparse.ArgumentParser(description="Replay exits against recorded P&L paths")
    ap.add_argument("--db", default="data/glassbox.db")
    ap.add_argument("--audit-dir", default="audit")
    ap.add_argument("--days", nargs="+", required=True, help="YYYY-MM-DD, one or more")
    ap.add_argument("--set", dest="overrides", nargs="*", default=[],
                    help="section.key=value — shown against the unmodified config")
    args = ap.parse_args()

    cfg = load_config()
    store = Store(args.db)
    days = [date.fromisoformat(d) for d in args.days]
    deadline = None
    try:
        deadline = datetime.fromisoformat(cfg.manage.flatten_all_at)
    except (TypeError, ValueError):
        pass

    print(render(replay_exits(store, args.audit_dir, days, cfg, deadline), "as configured"))
    if not args.overrides:
        return 0

    variant = cfg
    for override in args.overrides:
        try:
            path, raw = override.split("=", 1)
            section, key = path.split(".", 1)
        except ValueError:
            print(f"bad override {override!r}; expected section.key=value")
            return 2
        current = getattr(getattr(variant, section), key)
        value = raw.lower() == "true" if isinstance(current, bool) else type(current)(raw)
        variant = variant.model_copy(update={
            section: getattr(variant, section).model_copy(update={key: value})
        })
    print("\n" + render(replay_exits(store, args.audit_dir, days, variant, deadline),
                        ", ".join(args.overrides)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
