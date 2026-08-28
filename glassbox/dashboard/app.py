"""Read-only dashboard.

Deliberately shows only what Alpaca's own dashboard cannot: the reasoning behind
each decision, the trades that were refused and never submitted, risk-budget
utilisation against our own caps, and the state of the learning components.
Equity, positions and the order blotter are the broker's job and are linked to
rather than rebuilt.

The page has no controls. It is served publicly as the demo URL, so it can read
state and nothing else — the kill switch and every other control lives in the
CLI on the host. That is a deliberate security property, not an omission.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

from glassbox.clock import now_utc
from glassbox.config import load_config
from glassbox.dashboard.audit_reader import (
    audit_chain_status,
    build_decisions,
    pnl_by_arm,
    read_records,
    summarise_vetoes,
)
from glassbox.ml.bandit import ThompsonBandit
from glassbox.ml.metalabel import MetaLabeler
from glassbox.portfolio import snapshot
from glassbox.reconcile import HALT_KEY
from glassbox.store import Store

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="GlassBox AI Quant", docs_url=None, redoc_url=None)


def _cfg():
    return load_config()


def _store() -> Store:
    return Store(_cfg().paths.db)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state() -> dict:
    """Everything the panels need, in one call."""
    cfg = _cfg()
    store = _store()
    try:
        equity = cfg.account.starting_equity
        stored_equity = store.get_state("last_equity")
        if stored_equity:
            equity = float(stored_equity)

        portfolio = snapshot(store)
        heat_cap = equity * cfg.risk.portfolio_heat_pct / 100
        meta = MetaLabeler.load(
            Path(cfg.paths.models_dir) / "metalabel.pkl",
            min_samples=cfg.ml.min_training_samples,
        )
        bandit = ThompsonBandit(store)
        halt = store.get_state(HALT_KEY) or ""
        heartbeat = store.get_state("trader_heartbeat")

        heartbeat_age = None
        if heartbeat:
            from datetime import datetime

            heartbeat_age = (now_utc() - datetime.fromisoformat(heartbeat)).total_seconds()

        return {
            "generated_at": now_utc().isoformat(),
            "risk": {
                "heat": portfolio.heat,
                "heat_cap": heat_cap,
                "heat_pct_of_cap": 100 * portfolio.heat / heat_cap if heat_cap else 0.0,
                "open_positions": portfolio.open_position_count,
                "delta_dollars": portfolio.greeks.delta_dollars,
                "delta_band": cfg.risk.delta_dollars_band,
                "positions_by_underlying": portfolio.positions_by_underlying,
                "r_per_trade_pct": cfg.risk.r_per_trade_pct,
                "daily_loss_halt_pct": cfg.risk.daily_loss_halt_pct,
                "max_drawdown_halt_pct": cfg.risk.max_drawdown_halt_pct,
            },
            "learning": {
                "meta_labeler": {
                    "trained": meta.is_trained,
                    "n_samples": meta.n_samples,
                    "min_samples": meta.min_samples,
                    "trained_at": meta.trained_at,
                },
                "bandit": bandit.summary(),
                "pnl_by_arm": pnl_by_arm(store),
            },
            "health": {
                "halted": bool(halt),
                "halt_reason": halt,
                "heartbeat_age_seconds": heartbeat_age,
                "audit_chain": audit_chain_status(cfg.paths.audit_dir),
            },
        }
    finally:
        store.close()


@app.get("/api/decisions")
def decisions(limit: int = 60) -> dict:
    records = read_records(_cfg().paths.audit_dir)
    return {"decisions": [d.as_dict() for d in build_decisions(records)[:limit]]}


@app.get("/api/vetoes")
def vetoes() -> dict:
    return summarise_vetoes(read_records(_cfg().paths.audit_dir)).as_dict()


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    """Server-sent events. Polls the audit log for new records and pushes them.

    Polling a file is unglamorous but it has no shared state with the trader —
    the dashboard can crash, restart, or be scraped without touching the process
    that is holding positions.
    """

    async def events():
        seen: set[str] = set()
        first = True
        while True:
            try:
                records = read_records(_cfg().paths.audit_dir, days=1)
                fresh = [r for r in records if r.get("record_id") not in seen]
                seen.update(r.get("record_id") for r in records)
                if first:
                    first = False  # do not replay history on connect
                elif fresh:
                    yield f"data: {json.dumps({'records': fresh[-20:]})}\n\n"
                else:
                    yield ": keepalive\n\n"
            except Exception as e:  # noqa: BLE001 -- the stream must outlive any
                # transient read error; the page reconnects on its own otherwise.
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main() -> int:
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "glassbox.dashboard.app:app",
        host=cfg.dashboard.host,
        port=cfg.dashboard.port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
