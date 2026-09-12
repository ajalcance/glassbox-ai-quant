"""Square the store with the broker after an out-of-band close.

The supervisor's emergency flatten talks straight to the broker. It has to —
the whole reason it is firing is that the trader can no longer be trusted to
act, so routing the close through the trader's bookkeeping would be routing it
through the thing that is broken. The cost of that design is a divergence: the
positions are gone at the broker and still `open` in the store, and the next
reconcile pass halts on `local-only`, exactly as invariant 10 requires.

That halt is correct and must stay correct. What was missing is the repair —
the supported way to tell the store what the broker already did, so the halt
clears because the divergence is *explained*, not because someone reached in
and deleted the flag.

Two rules make this safe to run unattended:

  * The broker is truth. A position is only repaired when the broker holds
    NONE of its legs. Any leg still on the book means the flatten was partial,
    and a partial flatten is a situation for a person, not a script.
  * The exit price comes from the broker's own fills, never from a mark, a
    mid, or an assumption. If the fills cannot be matched unambiguously to one
    position, the position is skipped and said so out loud.

The second rule matters more than it looks: two positions that share an option
symbol cannot have their fills told apart by symbol alone, so this refuses
rather than guesses. See `gate._check_leg_conflict`, which exists to stop that
overlap from being created in the first place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

REPAIR_BARRIER = "supervisor_flatten"
REPAIRABLE_STATUSES = ("open", "opening", "closing")
AUDIT_ROLE = "repair"


@dataclass(frozen=True, slots=True)
class Resolution:
    """What we propose to do about one stranded position, and why."""

    position_id: str
    underlying: str
    ok: bool
    reason: str
    close_price: float | None = None
    realized_pnl: float | None = None
    fills: tuple[str, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        if not self.ok:
            return f"SKIP  {self.position_id} ({self.underlying}) — {self.reason}"
        return (
            f"CLOSE {self.position_id} ({self.underlying}) at {self.close_price:+.2f} "
            f"→ realised {self.realized_pnl:+.2f}"
        )


def _legs(row) -> list[dict]:
    return json.loads(row["legs_json"] or "[]")


def _closing_side(leg_side: str) -> str:
    """The side of the fill that would have closed a leg we hold.

    Alpaca reports option closes as plain `buy` / `sell` (the `_to_open` /
    `_to_close` intent appears on the order, not the activity), so this
    matches on the direction only.
    """
    return "sell" if leg_side == "long" else "buy"


def plan(rows, broker_leg_quantities: dict[str, int], fills) -> list[Resolution]:
    """Decide what each stranded position's exit was. Pure — no I/O, no writes.

    `rows` are store position rows; `broker_leg_quantities` is signed contract
    count per option symbol as the broker reports it; `fills` are
    `activities.Fill` records covering at least the period since the positions
    opened.
    """
    candidates = [r for r in rows if r["status"] in REPAIRABLE_STATUSES]

    # A symbol claimed by two stranded positions makes its fills unattributable.
    claims: dict[str, int] = {}
    for row in candidates:
        for leg in _legs(row):
            claims[leg["symbol"]] = claims.get(leg["symbol"], 0) + 1

    by_symbol: dict[str, list] = {}
    for f in fills:
        by_symbol.setdefault(f.symbol, []).append(f)

    out: list[Resolution] = []
    for row in candidates:
        pid, underlying = row["position_id"], row["underlying"]
        legs = _legs(row)
        qty = int(row["qty"] or 0)

        still_held = [
            leg["symbol"] for leg in legs if broker_leg_quantities.get(leg["symbol"], 0) != 0
        ]
        if still_held:
            out.append(
                Resolution(
                    pid,
                    underlying,
                    False,
                    f"broker still holds {', '.join(sorted(still_held))} — "
                    "flatten was partial, not a repair case",
                )
            )
            continue

        shared = [leg["symbol"] for leg in legs if claims.get(leg["symbol"], 0) > 1]
        if shared:
            out.append(
                Resolution(
                    pid,
                    underlying,
                    False,
                    f"legs {', '.join(sorted(shared))} are shared with another open "
                    "position — fills cannot be attributed unambiguously",
                )
            )
            continue

        if qty <= 0:
            out.append(Resolution(pid, underlying, False, f"position qty is {qty}"))
            continue

        opened_at = str(row["opened_at"] or "")
        net_cash = 0.0
        matched: list[str] = []
        missing: list[str] = []
        for leg in legs:
            want = _closing_side(leg["side"])
            need = int(leg["ratio_qty"]) * qty
            hits = [
                f
                for f in by_symbol.get(leg["symbol"], [])
                if f.side.startswith(want) and f.at > opened_at
            ]
            filled = sum(f.qty for f in hits)
            if filled != need:
                missing.append(f"{leg['symbol']} ({filled}/{need} closed)")
                continue
            net_cash += sum(f.signed_cash * f.qty for f in hits)
            matched.extend(f"{f.symbol} {f.side} {f.qty}@{f.price}" for f in hits)

        if missing:
            out.append(
                Resolution(
                    pid,
                    underlying,
                    False,
                    "no matching close fills for " + "; ".join(missing),
                )
            )
            continue

        # Same orientation as lifecycle._on_fill: positive means we paid to get
        # out, negative means the exit paid us.
        close_price = net_cash / qty
        entry = float(row["entry_price"] or 0.0)
        realized = (-close_price - entry) * 100 * qty
        out.append(
            Resolution(
                pid,
                underlying,
                True,
                f"closed out-of-band at {close_price:+.2f} against entry {entry:+.2f}",
                close_price=close_price,
                realized_pnl=realized,
                fills=tuple(matched),
            )
        )
    return out


def apply(store, audit, resolutions, closed_at: datetime) -> int:
    """Write the resolved exits. Returns how many positions were closed.

    Unresolved entries are deliberately left alone: they keep the reconcile
    halt standing, which is the right outcome for a divergence nobody has
    explained yet.
    """
    written = 0
    for r in resolutions:
        if not r.ok or r.realized_pnl is None:
            continue
        # No meta-label, on purpose. A position liquidated because the trader
        # process died says nothing about whether the signal was any good, and
        # `store.training_rows()` selects on `meta_label IS NOT NULL` — so a
        # NULL here keeps an infrastructure failure out of the training set
        # instead of teaching the model that this setup loses money. The
        # bandit is not fed either, for the same reason.
        store.close_position(
            r.position_id, REPAIR_BARRIER, None, r.realized_pnl, closed_at.isoformat()
        )
        audit.append(
            "repair_close",
            {
                "position_id": r.position_id,
                "underlying": r.underlying,
                "barrier": REPAIR_BARRIER,
                "close_price": r.close_price,
                "realized_pnl": r.realized_pnl,
                "fills": list(r.fills),
                "reason": r.reason,
            },
        )
        written += 1
    return written


def _cli() -> int:
    """Inspect, and only on --apply, repair.

    Reporting is the default because this writes to the live position ledger.
    Seeing the proposed exits and their prices before they are booked is the
    difference between a repair and a second incident.
    """
    import argparse

    from glassbox.audit import AuditLog
    from glassbox.clock import now_utc
    from glassbox.config import load_config, require_env
    from glassbox.data.activities import fills_since
    from glassbox.data.alpaca_client import trading_client
    from glassbox.reconcile import _broker_leg_quantities
    from glassbox.store import Store

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the exits (default: report only)")
    ap.add_argument("--since", help="ISO timestamp to search fills from (default: 3 days ago)")
    args = ap.parse_args()

    cfg = load_config()
    store = Store(cfg.paths.db)
    rows = store.open_positions()
    if not rows:
        print("nothing to repair — no open positions in the store")
        return 0

    client = trading_client()
    broker = _broker_leg_quantities(client.get_all_positions())

    from datetime import timedelta

    since = (
        datetime.fromisoformat(args.since)
        if args.since
        else now_utc() - timedelta(days=3)
    )
    fills = fills_since(
        require_env("ALPACA_API_KEY_ID"), require_env("ALPACA_API_SECRET_KEY"), since
    )

    resolutions = plan(rows, broker, fills)
    for r in resolutions:
        print(f"  {r}")

    resolved = [r for r in resolutions if r.ok]
    total = sum(r.realized_pnl or 0.0 for r in resolved)
    print(f"\n{len(resolved)}/{len(resolutions)} repairable, net realised {total:+.2f}")

    if not args.apply:
        print("report only — re-run with --apply to write these exits")
        return 0
    if not resolved:
        print("nothing to write")
        return 1

    # Its own role, like every other out-of-process tool. Each role owns
    # `YYYY-MM-DD-<role>.jsonl` exclusively (see audit.py), and this runs while
    # the trader is live: sharing the file forks the hash chain, because two
    # writers each chain from the last record THEY wrote. Done exactly once, on
    # 12 Sep, by this CLI. The fork stands in that day's file — a broken chain
    # is evidence, and editing it to look clean is the one thing the chain
    # exists to make impossible.
    audit = AuditLog(cfg.paths.audit_dir, role=AUDIT_ROLE)
    written = apply(store, audit, resolutions, now_utc())
    store.close()
    print(f"wrote {written} exit(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
