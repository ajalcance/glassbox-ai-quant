"""PASSIVE invariant monitor — runs alongside the live session, read-only.

    uv run python soak/monitor_passive.py --account YOUR_PAPER_ACCOUNT                 # host-run stack
    uv run python soak/monitor_passive.py --account YOUR_PAPER_ACCOUNT --source docker # dockerized stack

Every cycle it verifies, without ever writing to production state:

  chains        every per-role audit hash chain for today verifies
  reconcile     local open positions agree with the broker (pure compare)
  heat          store heat == Σ max_loss of open positions, and under the cap
  dup_coids     no duplicate client_order_ids among live broker orders, and
                no unknown gbx-* order at the broker
  transitions   position status only ever moves along legal edges
  heartbeat     trader heartbeat fresh (< 90s) while the market is open
  processes     trader / supervisor / scheduler actually running

State reads go through a consistent SQLite snapshot (backup API from a
read-only connection); in docker mode the snapshot is taken inside the
container and copied out. Findings append to soak-results/ and a live
view streams over SSE at http://127.0.0.1:8899 — this is soak tooling, fully
separate from the public dashboard.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter, deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from glassbox.audit import day_files, verify_day
from glassbox.clock import market_date
from glassbox.config import load_config
from glassbox.data.alpaca_client import trading_client
from glassbox.reconcile import reconcile
from glassbox.store import Store

# --account is REQUIRED and has no default: the monitor is read-only, but a
# monitor silently watching the wrong account reports "all invariants hold"
# about a system nobody is running.
HEARTBEAT_STALE_S = 90.0  # matches the supervisor guard default
# A defined-risk spread cannot realise much beyond its own max loss: a debit
# spread's best case is roughly the width above the debit paid (~1x), and a
# credit spread's is the credit itself. 1.5 leaves 50% headroom for slippage
# and fees while still catching a sign inversion, which doubles the magnitude.
# Calibrated against the real incident: $497 realised on a $247 max-loss
# position is a ratio of 2.01, so a factor of 3.0 — the first value written
# here — would have slept through the exact bug this exists to catch.
PNL_PLAUSIBILITY_FACTOR = 1.5
# Swallowed errors per day before it stops being noise. Every one of these is
# caught by design so a single bad event cannot end a session, which is exactly
# why they need counting.
ERROR_BUDGET = 25
# How long the pipeline may evaluate nothing, mid-session, before that is
# treated as silence rather than a quiet tape.
SILENCE_MINUTES = 45
LEGAL_TRANSITIONS = {
    ("opening", "open"),
    ("opening", "closed"),
    ("open", "closing"),
    ("open", "closed"),
    ("closing", "closed"),
}
CONTAINERS = ("glassbox-trader", "glassbox-supervisor", "glassbox-scheduler")
PROCESS_PATTERNS = ("glassbox.runner", "glassbox.supervisor", "glassbox.scheduler")


# --------------------------------------------------------------------------
# state snapshot
# --------------------------------------------------------------------------

def snapshot_db_local(db_path: Path, out_path: Path) -> bool:
    """Consistent copy via the backup API, from a read-only connection so the
    monitor cannot write production state even by accident."""
    if not db_path.exists():
        return False
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(out_path)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    return True


def snapshot_docker(workdir: Path) -> tuple[Path | None, Path | None]:
    """DB snapshot + audit copy out of the trader container (named volumes are
    not host-readable on macOS). Returns (db_copy, audit_dir) or Nones."""
    db_out = workdir / "docker-snap.db"
    audit_out = workdir / "docker-audit"
    snap_script = (
        "import sqlite3; s=sqlite3.connect('file:/app/data/glassbox.db?mode=ro',uri=True); "
        "d=sqlite3.connect('/tmp/gbx-snap.db'); s.backup(d); d.close(); s.close()"
    )
    code = subprocess.run(
        ["docker", "exec", "glassbox-trader", "python", "-c", snap_script],
        capture_output=True, text=True, check=False,
    )
    db_ok = code.returncode == 0
    if db_ok:
        db_ok = subprocess.run(
            ["docker", "cp", "glassbox-trader:/tmp/gbx-snap.db", str(db_out)],
            capture_output=True, check=False,
        ).returncode == 0
    shutil.rmtree(audit_out, ignore_errors=True)
    audit_ok = subprocess.run(
        ["docker", "cp", "glassbox-trader:/app/audit", str(audit_out)],
        capture_output=True, check=False,
    ).returncode == 0
    return (db_out if db_ok else None), (audit_out if audit_ok else None)


def _minutes_ago(ts: str | None) -> float:
    """Age of an ISO timestamp in minutes; unparseable reads as ancient, so a
    malformed record can never make the pipeline look alive."""
    if not ts:
        return float("inf")
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds() / 60
    except ValueError:
        return float("inf")


def supervisor_log_age() -> float | None:
    """Seconds since the supervisor last printed anything. It reports every
    tick, so silence means frozen — even while docker says 'running'."""
    out = subprocess.run(
        ["docker", "logs", "--tail", "1", "--timestamps", "glassbox-supervisor"],
        capture_output=True, text=True, check=False,
    )
    line = (out.stdout.strip() or out.stderr.strip()).splitlines()
    if not line:
        return None
    stamp = line[-1].split(" ", 1)[0]
    with contextlib.suppress(ValueError):
        ts = datetime.fromisoformat(stamp)
        return (datetime.now(UTC) - ts).total_seconds()
    return None


def docker_resources() -> dict:
    """Container memory percentages and host root-disk usage — the endurance
    signals that reveal leaks and growth long before they become outages."""
    res: dict = {"mem_pct": {}}
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemPerc}}"],
        capture_output=True, text=True, check=False,
    ).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("glassbox-"):
            with contextlib.suppress(ValueError):
                res["mem_pct"][parts[0]] = float(parts[1].rstrip("%"))
    df = subprocess.run(["df", "-P", "/"], capture_output=True, text=True, check=False).stdout
    lines = df.splitlines()
    if len(lines) >= 2:
        with contextlib.suppress(ValueError, IndexError):
            res["disk_pct"] = int(lines[1].split()[4].rstrip("%"))
    return res


def liveness(source: str) -> dict[str, bool]:
    if source == "docker":
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.Name}} {{.State.Running}}", *CONTAINERS],
            capture_output=True, text=True, check=False,
        ).stdout
        up = {line.split()[0].lstrip("/"): line.split()[1] == "true"
              for line in out.splitlines() if line.strip()}
        return {c: up.get(c, False) for c in CONTAINERS}
    alive = {}
    for pat in PROCESS_PATTERNS:
        found = subprocess.run(
            ["pgrep", "-f", pat], capture_output=True, text=True, check=False
        ).stdout
        alive[pat.split(".")[-1]] = bool(found.strip())
    return alive


# --------------------------------------------------------------------------
# checks — each returns (ok, detail)
# --------------------------------------------------------------------------

class Monitor:
    def __init__(self, args):
        self.args = args
        self.cfg = load_config()
        self.client = trading_client()
        self.workdir = ROOT / "soak-results"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.findings_path = self.workdir / "monitor-findings.jsonl"
        self.samples_path = self.workdir / "samples.jsonl"
        self.samples: deque = deque(maxlen=720)  # ~4h at 20s, for the sparkline
        self.prev_status: dict[str, str] = {}
        self.pending_unknown: set[str] = set()
        self.pending_orphans: set[str] = set()
        self._fired: set[str] = set()
        self.findings: list[dict] = []
        self.snapshot_lock = threading.Lock()
        self.snapshot: dict = {"cycle": 0, "checks": {}, "findings": []}
        self.clients: list[queue.Queue] = []
        self.clients_lock = threading.Lock()

    # -- findings ----------------------------------------------------------
    def finding(self, severity: str, check: str, detail: str) -> None:
        key = f"{check}|{detail}"
        if key in self._fired:
            return  # already reported and unchanged; do not spam every cycle
        self._fired.add(key)
        row = {"ts": datetime.now(UTC).isoformat(), "severity": severity,
               "check": check, "detail": detail}
        self.findings.append(row)
        with open(self.findings_path, "a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"  !! [{severity}] {check}: {detail}")
        if severity in ("CRITICAL", "FAIL"):
            self.push(f"[{severity}] {check}", detail)

    def push(self, title: str, body: str) -> None:
        """Best-effort phone alert via ntfy.sh — a HALT at 2 AM that only a
        local findings file knows about is a HALT nobody acts on. Fire and
        forget on a thread; alerting must never slow or break a check cycle."""
        topic = self.args.ntfy_topic
        if not topic:
            return

        def _send():
            with contextlib.suppress(Exception):
                req = urllib.request.Request(
                    f"https://ntfy.sh/{topic}",
                    data=body.encode()[:2000],
                    headers={"Title": title, "Priority": "high", "Tags": "rotating_light"},
                )
                urllib.request.urlopen(req, timeout=10)

        threading.Thread(target=_send, daemon=True).start()

    def clear(self, check: str) -> None:
        """A check that is OK again may fire fresh findings later."""
        self._fired = {k for k in self._fired if not k.startswith(f"{check}|")}

    # -- one cycle ---------------------------------------------------------
    def cycle(self, n: int) -> dict:
        checks: dict[str, dict] = {}
        metrics: dict = {}
        # Market state is needed by several checks; read it once, and treat an
        # unreadable clock as "closed" so a data outage cannot itself raise a
        # false alarm about silence.
        market_is_open = False
        with contextlib.suppress(Exception):
            market_is_open = bool(self.client.get_clock().is_open)

        def record(name, ok, detail, severity="FAIL"):
            checks[name] = {"ok": bool(ok), "detail": str(detail)[:400]}
            if ok:
                self.clear(name)
            else:
                self.finding(severity, name, str(detail)[:400])

        # --- gather state
        if self.args.source == "docker":
            db_copy, audit_dir = snapshot_docker(self.workdir)
        else:
            db_copy = self.workdir / "local-snap.db"
            if not snapshot_db_local(ROOT / self.cfg.paths.db, db_copy):
                db_copy = None
            audit_dir = ROOT / self.cfg.paths.audit_dir

        # --- audit chains
        try:
            if audit_dir and Path(audit_dir).exists():
                ok, total, broken = verify_day(audit_dir)
                record("chains", ok,
                       f"{total} records across today's role files" if ok
                       else f"BROKEN: {broken}", severity="CRITICAL")
            else:
                record("chains", True, "no audit dir yet")
        except Exception as e:  # noqa: BLE001
            record("chains", False, f"verify failed: {type(e).__name__}: {e}")

        # Today's audit records, read once for the regression-watch checks
        # below. Malformed lines are skipped: a monitor must never be the
        # reason you cannot see the system.
        recent_audit: list[dict] = []
        with contextlib.suppress(Exception):
            for path in day_files(audit_dir):
                for line in path.read_text(errors="replace").splitlines():
                    with contextlib.suppress(json.JSONDecodeError):
                        recent_audit.append(json.loads(line))

        # --- broker reads (read-only API calls)
        account = positions = live_orders = None
        try:
            account = self.client.get_account()
            positions = self.client.get_all_positions()
            from alpaca.trading.requests import GetOrdersRequest

            live_orders = self.client.get_orders(GetOrdersRequest(status="open", limit=500))
        except Exception as e:  # noqa: BLE001
            record("broker", False, f"broker read failed: {type(e).__name__}: {e}", "WARN")

        store = Store(db_copy) if db_copy else None
        try:
            # --- reconcile agreement
            if store and positions is not None:
                rec = reconcile(store, positions)
                record("reconcile", rec.ok, rec.reason, severity="CRITICAL")
                halt = store.get_state("halt_reason")
                metrics["halted"] = bool(halt)
                start = float(store.get_state("session_start_equity") or 0)
                peak = float(store.get_state("peak_equity") or 0)
                if account is not None:
                    eq = float(account.equity)
                    metrics["daily_pnl_pct"] = 100 * (eq - start) / start if start else None
                    metrics["drawdown_pct"] = 100 * (eq - peak) / peak if peak else None
                checks["halt"] = {"ok": not halt, "detail": halt or "not halted"}
                if halt:
                    self.finding("WARN", "halt", f"system halted: {halt}")
                else:
                    self.clear("halt")
            elif not store:
                record("reconcile", True, "no db snapshot yet (stack not started?)")

            # --- heat
            if store:
                rows = store.open_positions()
                manual = sum(float(r["max_loss"]) for r in rows)
                heat = store.total_heat()
                metrics["heat"] = heat
                metrics["local_open_positions"] = len(rows)
                agree = abs(heat - manual) < 0.01
                detail = f"heat=${heat:,.0f} over {len(rows)} open position(s)"
                if account is not None:
                    cap = float(account.equity) * self.cfg.risk.portfolio_heat_pct / 100
                    agree = agree and heat <= cap + 0.01
                    detail += f", cap=${cap:,.0f}"
                record("heat", agree, detail if agree else
                       f"heat=${heat:,.2f} vs Σmax_loss=${manual:,.2f}, {detail}",
                       severity="CRITICAL")

            # --- duplicate / unknown live client_order_ids
            if store and live_orders is not None:
                coids = Counter(o.client_order_id for o in live_orders if o.client_order_id)
                dups = {c: k for c, k in coids.items() if k > 1}
                record("dup_coids", not dups,
                       f"{len(coids)} live order id(s), all unique" if not dups
                       else f"DUPLICATE live client_order_ids: {dups}", severity="CRITICAL")
                unknown = {c for c in coids if c.startswith("gbx-") and not store.get_order(c)}
                confirmed = unknown & self.pending_unknown  # two consecutive sightings
                self.pending_unknown = unknown
                if confirmed:
                    self.finding("WARN", "unknown_orders",
                                 f"broker holds gbx-* orders missing from the store: "
                                 f"{sorted(confirmed)}")
                checks["unknown_orders"] = {
                    "ok": not confirmed,
                    "detail": "all live orders known to the store" if not confirmed
                    else f"{len(confirmed)} unknown",
                }

            # --- REGRESSION WATCH -------------------------------------------
            # One detector per bug actually shipped to this system. Each would
            # have caught its bug in production before a human noticed, and
            # each stays armed in case a later change reintroduces it.

            # 1. Close-sign inversion. A realised P&L larger than the position
            # could possibly lose means the fill was read in the wrong
            # orientation — the bug that would have booked a -$3 trade as -$497.
            if store:
                implausible = []
                for r in store.training_rows():
                    pnl, cap = r["realized_pnl"], r["max_loss"]
                    if pnl is None or cap is None or float(cap) <= 0:
                        continue
                    # A defined-risk spread cannot lose more than max_loss, and
                    # cannot win more than roughly the width beyond it either.
                    if abs(float(pnl)) > float(cap) * PNL_PLAUSIBILITY_FACTOR:
                        implausible.append(
                            f"{r['position_id']}: pnl=${float(pnl):,.0f} vs max_loss=${float(cap):,.0f}"
                        )
                record("pnl_plausible", not implausible,
                       f"{len(store.training_rows())} closed position(s), all within max_loss"
                       if not implausible
                       else f"IMPLAUSIBLE realised P&L (close-sign regression?): {implausible}",
                       severity="CRITICAL")

            # 2. Orphaned exits. A position stuck in 'closing' or 'opening'
            # with no in-flight order is unreachable: the manager only walks
            # 'open', so no barrier and no deadline flatten will ever touch it.
            if store:
                in_flight_positions = {
                    o["position_id"] for o in store.orders_in_flight() if o["position_id"]
                }
                orphans = [
                    f"{r['position_id']} ({r['status']})"
                    for r in store.open_positions()
                    if r["status"] in ("opening", "closing")
                    and r["position_id"] not in in_flight_positions
                ]
                # One cycle of lag is normal between submit and the store write;
                # only a repeat sighting is a real orphan.
                confirmed_orphans = set(orphans) & self.pending_orphans
                self.pending_orphans = set(orphans)
                record("no_orphans", not confirmed_orphans,
                       "no positions stranded without an order"
                       if not confirmed_orphans
                       else f"ORPHANED (unreachable by the manager): {sorted(confirmed_orphans)}",
                       severity="CRITICAL")

            # 3. Daily baseline staleness. The -2% halt is only daily if its
            # baseline rolls over; written once it silently becomes "loss
            # since first boot" and day 2 inherits day 1's drawdown.
            if store:
                stamp = store.get_state("session_start_date")
                today = market_date().isoformat()
                fresh = stamp == today
                record("daily_baseline", fresh or not market_is_open,
                       f"session baseline dated {stamp} (today {today})"
                       + ("" if market_is_open else " — market closed, rolls at next open"),
                       severity="CRITICAL")

            # 4. One signal, one position. The durable dedupe behind the
            # in-memory seen-set: a restart replaying news must not open a
            # second position on a story already traded.
            if store:
                sids = Counter(
                    r["signal_id"] for r in store.open_positions() + store.training_rows()
                    if r["signal_id"]
                )
                repeats = {s: n for s, n in sids.items() if n > 1}
                record("signal_uniqueness", not repeats,
                       f"{len(sids)} signal(s), one position each" if not repeats
                       else f"DUPLICATE positions per signal: {repeats}", severity="CRITICAL")

            # 5. Per-position risk cap. Sizing haircuts were a silent no-op
            # whenever the volatility budget bound; a position above the cap
            # means a haircut failed to apply somewhere.
            if store and account is not None:
                cap = float(account.equity) * self.cfg.risk.max_loss_per_position_pct / 100
                oversized = [
                    f"{r['position_id']}: ${float(r['max_loss']):,.0f}"
                    for r in store.open_positions()
                    if r["max_loss"] and float(r["max_loss"]) > cap + 0.01
                ]
                record("position_cap", not oversized,
                       f"all positions within ${cap:,.0f} per-position cap" if not oversized
                       else f"OVERSIZED positions: {oversized}", severity="CRITICAL")

            # 6. A flatten that could not finish. The supervisor writes this
            # when legs remain after every retry — the guards decided
            # everything must go and something is still on the book.
            incomplete = [r for r in recent_audit if r.get("kind") == "flatten_incomplete"]
            record("flatten_complete", not incomplete,
                   "no incomplete flatten today" if not incomplete
                   else f"FLATTEN LEFT POSITIONS ON THE BOOK: {incomplete[-1].get('remaining')}",
                   severity="CRITICAL")

            # 7. Error-rate watch. These are all swallowed by design so one bad
            # event cannot end a session — which also means they are invisible
            # unless something counts them.
            err_kinds = ("pipeline_error", "lifecycle_error", "reconcile_error", "order_error",
                         "stream_error", "news_poll_error", "floor_error", "llm_error")
            errs = Counter(r["kind"] for r in recent_audit if r.get("kind") in err_kinds)
            record("error_rate", sum(errs.values()) <= ERROR_BUDGET,
                   f"{sum(errs.values())} swallowed error(s) today"
                   + (f": {dict(errs)}" if errs else ""),
                   severity="WARN")

            # 8. Silence detection. A system that stops seeing news looks
            # exactly like a quiet market, and the contest is scored on a
            # window too short to lose half of.
            if market_is_open:
                seen = sum(
                    1 for r in recent_audit
                    if r.get("kind") in ("analyst_view", "signal_dropped")
                    and _minutes_ago(r.get("ts")) <= SILENCE_MINUTES
                )
                record("pipeline_alive", seen > 0,
                       f"{seen} signal(s) evaluated in the last {SILENCE_MINUTES}min"
                       if seen else
                       f"NO news evaluated in {SILENCE_MINUTES}min while the market is open",
                       severity="CRITICAL")

            # --- status transitions
            if store:
                bad = []
                for r in store.open_positions() + store.training_rows():
                    pid, status = r["position_id"], r["status"]
                    prev = self.prev_status.get(pid)
                    if prev and prev != status and (prev, status) not in LEGAL_TRANSITIONS:
                        bad.append(f"{pid}: {prev}→{status}")
                    self.prev_status[pid] = status
                record("transitions", not bad,
                       f"{len(self.prev_status)} position(s) tracked" if not bad
                       else f"ILLEGAL transitions: {bad}", severity="CRITICAL")

            # --- heartbeat freshness
            if store:
                stamp = store.get_state("trader_heartbeat")
                if stamp:
                    age = (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
                    market_open = False
                    with contextlib.suppress(Exception):
                        market_open = bool(self.client.get_clock().is_open)
                    stale = age > HEARTBEAT_STALE_S
                    record("heartbeat", not (stale and market_open),
                           f"trader heartbeat {age:.0f}s ago"
                           + ("" if market_open else " (market closed)"),
                           severity="FAIL")
                else:
                    checks["heartbeat"] = {"ok": True, "detail": "trader has not started yet"}
        finally:
            if store:
                store.close()

        # --- processes / containers
        alive = liveness(self.args.source)
        down = [k for k, v in alive.items() if not v]
        record("processes", not down,
               ", ".join(f"{k}:{'up' if v else 'DOWN'}" for k, v in alive.items()),
               severity="WARN")

        # --- a hung process looks "running" to docker; the supervisor prints
        # every tick, so a silent supervisor log is a frozen watchdog.
        if self.args.source == "docker" and alive.get("glassbox-supervisor"):
            age = supervisor_log_age()
            record("supervisor_fresh", age is not None and age < 60,
                   f"last supervisor log line {age:.0f}s ago" if age is not None
                   else "could not read supervisor log timestamp",
                   severity="CRITICAL")

        # --- resource trend (endurance data): container memory + host disk
        if self.args.source == "docker":
            res = docker_resources()
            metrics.update(res)
            worst_mem = max(res.get("mem_pct", {}).values(), default=0.0)
            disk = res.get("disk_pct", 0)
            record("resources",
                   worst_mem < 85 and disk < 80,
                   f"container mem worst {worst_mem:.0f}%, disk {disk}%",
                   severity="WARN")

        sample = {
            "ts": datetime.now(UTC).isoformat(),
            "equity": float(account.equity) if account is not None else None,
            "heat": metrics.get("heat"),
            "local_open_positions": metrics.get("local_open_positions"),
            "broker_positions": len(positions) if positions is not None else None,
            "open_orders": len(live_orders) if live_orders is not None else None,
            "daily_pnl_pct": metrics.get("daily_pnl_pct"),
            "drawdown_pct": metrics.get("drawdown_pct"),
            "halted": metrics.get("halted"),
            "mem_pct": metrics.get("mem_pct"),
            "disk_pct": metrics.get("disk_pct"),
        }
        with open(self.samples_path, "a") as f:
            f.write(json.dumps(sample) + "\n")
        self.samples.append(sample)

        snap = {
            "cycle": n,
            "ts": datetime.now(UTC).isoformat(),
            "source": self.args.source,
            "equity": float(account.equity) if account is not None else None,
            "account": getattr(account, "account_number", None),
            "open_orders": len(live_orders) if live_orders is not None else None,
            "positions": len(positions) if positions is not None else None,
            "checks": checks,
            "findings": self.findings[-50:],
            "samples": [[s["ts"], s["equity"]] for s in self.samples if s["equity"]],
            "all_ok": all(c["ok"] for c in checks.values()),
        }
        with self.snapshot_lock:
            self.snapshot = snap
        self.broadcast(snap)
        return snap

    # -- SSE ---------------------------------------------------------------
    def broadcast(self, snap: dict) -> None:
        data = json.dumps(snap)
        with self.clients_lock:
            for q in list(self.clients):
                with contextlib.suppress(Exception):
                    q.put_nowait(data)

    def run(self) -> int:
        acct = self.client.get_account()
        if acct.account_number != self.args.account:
            print(f"REFUSING: account {acct.account_number} != expected {self.args.account}")
            return 2
        server = make_server(self)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"passive monitor — source={self.args.source}, account {acct.account_number}")
        print(f"live view:  http://127.0.0.1:{self.args.port}")
        print(f"findings:   {self.findings_path}")
        print(f"samples:    {self.samples_path}")
        print("alerts:     " + (f"ntfy.sh/{self.args.ntfy_topic}" if self.args.ntfy_topic
                                else "OFF (--ntfy-topic to enable phone alerts)"))
        n = 0
        try:
            while True:
                n += 1
                t0 = time.time()
                try:
                    snap = self.cycle(n)
                    bad = [k for k, v in snap["checks"].items() if not v["ok"]]
                    print(f"[{datetime.now(UTC):%H:%M:%S}] cycle {n}: "
                          + ("all invariants hold" if snap["all_ok"]
                             else f"FAILING: {', '.join(bad)}"))
                except Exception as e:  # noqa: BLE001 -- the monitor must outlive
                    # any single bad cycle; a dead monitor watches nothing.
                    print(f"[{datetime.now(UTC):%H:%M:%S}] cycle {n} errored: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                time.sleep(max(1.0, self.args.interval - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\nmonitor stopped")
            return 0


# --------------------------------------------------------------------------
# tiny SSE server (localhost only — soak tooling, not the public dashboard)
# --------------------------------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>GlassBox soak monitor</title>
<style>
 body{background:#0d1117;color:#c9d1d9;font:14px/1.5 ui-monospace,Menlo,monospace;
      margin:0;padding:24px}
 h1{font-size:16px;color:#e6edf3} .muted{color:#8b949e}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
 .card{border:1px solid #30363d;border-radius:8px;padding:10px 12px;background:#161b22}
 .card b{display:block;margin-bottom:2px}
 .ok{border-left:4px solid #2ea043} .bad{border-left:4px solid #f85149}
 .ok b::after{content:" ✓";color:#2ea043} .bad b::after{content:" ✗";color:#f85149}
 #findings{margin-top:18px} .f{padding:3px 0;border-bottom:1px dotted #21262d}
 .sev-CRITICAL{color:#f85149;font-weight:bold} .sev-FAIL{color:#f0883e}
 .sev-WARN{color:#d29922} .banner{padding:8px 12px;border-radius:8px;margin:12px 0;
 font-weight:bold} .banner.ok{background:#12261e;color:#2ea043}
 .banner.bad{background:#2d1215;color:#f85149}
</style></head><body>
<h1>GlassBox soak monitor <span class="muted" id="meta"></span></h1>
<div class="banner ok" id="banner">connecting…</div>
<div style="border:1px solid #30363d;border-radius:8px;background:#161b22;padding:8px 12px">
  <span class="muted">equity</span> <span id="eqnow"></span>
  <svg id="spark" width="100%" height="56" viewBox="0 0 600 56"
       preserveAspectRatio="none"><polyline id="sparkline" fill="none"
       stroke="#2ea043" stroke-width="1.5" points=""/></svg>
</div>
<div class="grid" id="cards" style="margin-top:10px"></div>
<div id="findings"><h1>findings</h1><div id="flist" class="muted">none yet</div></div>
<script>
const es = new EventSource('/events');
es.onmessage = (e) => {
  const s = JSON.parse(e.data);
  document.getElementById('meta').textContent =
    `— cycle ${s.cycle} · ${s.ts} · ${s.source} · ` +
    (s.equity ? `equity $${s.equity.toLocaleString()}` : '') +
    (s.positions !== null ? ` · ${s.positions} pos` : '') +
    (s.open_orders !== null ? ` · ${s.open_orders} live orders` : '');
  const banner = document.getElementById('banner');
  banner.textContent = s.all_ok ? 'ALL INVARIANTS HOLD' : 'INVARIANT FAILING';
  banner.className = 'banner ' + (s.all_ok ? 'ok' : 'bad');
  if (s.samples && s.samples.length > 1) {
    const vals = s.samples.map(p => p[1]);
    const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
    const pts = vals.map((v, i) =>
      `${(i / (vals.length - 1)) * 600},${52 - ((v - lo) / span) * 48}`).join(' ');
    document.getElementById('sparkline').setAttribute('points', pts);
    document.getElementById('eqnow').textContent =
      `$${vals[vals.length - 1].toLocaleString()} (range $${lo.toLocaleString()}–$${hi.toLocaleString()})`;
  }
  document.getElementById('cards').innerHTML = Object.entries(s.checks).map(
    ([k,v]) => `<div class="card ${v.ok?'ok':'bad'}"><b>${k}</b>` +
               `<span class="muted">${v.detail}</span></div>`).join('');
  if (s.findings.length) document.getElementById('flist').innerHTML =
    s.findings.slice().reverse().map(f =>
      `<div class="f"><span class="sev-${f.severity}">${f.severity}</span> ` +
      `<span class="muted">${f.ts}</span> <b>${f.check}</b> — ${f.detail}</div>`).join('');
};
es.onerror = () => { document.getElementById('banner').textContent =
  'stream lost — monitor down? retrying…'; };
</script></body></html>"""


def make_server(mon: Monitor) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console for invariant output
            pass

        def do_GET(self):
            if self.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/state.json":
                with mon.snapshot_lock:
                    body = json.dumps(mon.snapshot).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q: queue.Queue = queue.Queue(maxsize=10)
                with mon.snapshot_lock:
                    first = json.dumps(mon.snapshot)
                with mon.clients_lock:
                    mon.clients.append(q)
                try:
                    self.wfile.write(f"data: {first}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            data = q.get(timeout=30)
                            self.wfile.write(f"data: {data}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with mon.clients_lock, contextlib.suppress(ValueError):
                        mon.clients.remove(q)
            else:
                self.send_response(404)
                self.end_headers()

    return ThreadingHTTPServer(("127.0.0.1", mon.args.port), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="GlassBox passive invariant monitor")
    parser.add_argument("--source", choices=["local", "docker"], default="local",
                        help="where the stack runs: host processes or docker compose")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--account",
        required=True,
        help="Alpaca paper account number to watch; the monitor aborts if the "
        "credentials in .env resolve to any other account",
    )
    parser.add_argument("--ntfy-topic", default=os.environ.get("GLASSBOX_NTFY_TOPIC", ""),
                        help="ntfy.sh topic for phone alerts on CRITICAL/FAIL findings "
                        "(subscribe to the same topic in the ntfy app); empty = no alerts")
    args = parser.parse_args()
    return Monitor(args).run()


if __name__ == "__main__":
    sys.exit(main())
