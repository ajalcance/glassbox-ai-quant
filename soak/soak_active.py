"""ACTIVE soak harness — pre-open chaos drills against the DEV paper account.

Run from the repo root, before the open, on the dev account only:

    uv run python soak/soak_active.py --account YOUR_PAPER_ACCOUNT

Exercises the real glassbox code (imported from the repo) under the concurrent
conditions the test suite cannot create:

  news_storm    concurrent duplicate news through Runner.handle_news — proves
                the deterministic client_order_id survives the check-then-act
                race all the way to the broker
  sqlite        many concurrent Store writers + the exact cross-thread
                shared-connection pattern the trader's stream thread uses
  kill          kill-switch engaged while an order rests; supervisor tick
                (dry-run) must HALT_HARD; flips mid-submission stream
  crash         SIGKILL a submitting process at random points, rebuild state
                from the store + broker, prove the idempotent retry
  scheduler     scheduler-style jobs colliding with trader/supervisor ticks
                on shared SQLite + per-role audit chains

Never touches data/glassbox.db or audit/. Broker residue is canceled on exit;
run `make drill-clean` afterwards regardless (required before the 9:30 drills).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from glassbox.audit import AuditLog, verify_day
from glassbox.config import load_config
from glassbox.data.alpaca_client import trading_client
from glassbox.execution.ids import client_order_id
from glassbox.execution.router import OrderRouter
from glassbox.reconcile import reconcile
from glassbox.store import Store
from glassbox.structures import structure_key
from glassbox.verify_order import build_far_otm_put_spread

# --account is REQUIRED and has no default on purpose. This harness places real
# (unfillable, far-OTM) orders, so the account it may touch must be typed out
# every run — a default is exactly how a testing tool ends up pointed at the
# account you cannot afford it to touch.
RESTING_LIMIT = -4.50  # credit far above any real bid on a deep-OTM 5-wide: rests unfilled
STORM_THREADS = 12
STORM_ROUNDS = 4


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------

class Findings:
    """Console + JSONL sink. FAIL/CRITICAL drive the exit code."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, severity: str, scenario: str, check: str, detail: str) -> None:
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "severity": severity,
            "scenario": scenario,
            "check": check,
            "detail": detail,
        }
        with self._lock:
            self.rows.append(row)
            with open(self.path, "a") as f:
                f.write(json.dumps(row) + "\n")
        marker = {"PASS": "  ok ", "INFO": " info", "WARN": " WARN",
                  "FAIL": " FAIL", "CRITICAL": "CRIT!"}.get(severity, "  ?  ")
        print(f"  [{marker}] {check}: {detail}")

    def ok(self, scenario, check, detail):
        self.add("PASS", scenario, check, detail)

    def bad(self, condition: bool, scenario, check, ok_detail, fail_detail, severity="FAIL"):
        """Assert-style: condition True → PASS, else the given severity."""
        if condition:
            self.add("PASS", scenario, check, ok_detail)
        else:
            self.add(severity, scenario, check, fail_detail)

    @property
    def failed(self) -> bool:
        return any(r["severity"] in ("FAIL", "CRITICAL") for r in self.rows)


# --------------------------------------------------------------------------
# broker helpers
# --------------------------------------------------------------------------

def broker_orders_for(client, coid: str) -> list:
    """Every order at the broker carrying this client_order_id, any status."""
    from alpaca.trading.requests import GetOrdersRequest

    orders = client.get_orders(GetOrdersRequest(status="all", limit=500))
    return [o for o in orders if o.client_order_id == coid]


def cancel_soak_orders(client, coids: set[str], findings: Findings) -> None:
    """Sweep every live soak order. Residue would pollute the 9:30 drills."""
    from alpaca.trading.requests import GetOrdersRequest

    live = client.get_orders(GetOrdersRequest(status="open", limit=500))
    leftovers = [o for o in live if o.client_order_id in coids]
    for o in leftovers:
        with contextlib.suppress(Exception):
            client.cancel_order_by_id(o.id)
    if leftovers:
        print(f"  swept {len(leftovers)} resting soak order(s)")
    # An order the broker has accepted a cancel for is settling, not residue.
    # PENDING_CANCEL is a normal intermediate state and can persist for minutes
    # around the open, when the venue queues option cancels — counting it as
    # residue produced a CRITICAL on 1 Sep for fourteen orders whose cancels had
    # all been accepted. Only orders still genuinely working are residue.
    settling_states = {"pending_cancel", "pending_replace"}

    def outstanding():
        live = client.get_orders(GetOrdersRequest(status="open", limit=500))
        mine = [o for o in live if o.client_order_id in coids]
        resting, settling = [], []
        for o in mine:
            state = str(o.status).split(".")[-1].lower()
            (settling if state in settling_states else resting).append(o)
        return resting, settling

    resting, settling = [], []
    for _ in range(15):  # cancels are async at the broker; give them a moment
        resting, settling = outstanding()
        if not resting and not settling:
            break
        time.sleep(2)
    findings.bad(
        not resting, "cleanup", "no_soak_residue",
        "no soak orders left resting"
        + (f" ({len(settling)} cancel(s) still settling at the broker)" if settling else ""),
        f"{len(resting)} soak orders STILL WORKING — run `make drill-clean` and check the account",
        severity="CRITICAL",
    )


# --------------------------------------------------------------------------
# scenario: news_storm
# --------------------------------------------------------------------------

def scenario_news_storm(ctx) -> None:
    """The check-then-act race: socket and poller deliver the same story at the
    same moment. Downstream, everything funnels into one deterministic
    client_order_id — the broker must end up with exactly one order."""
    from types import SimpleNamespace

    from glassbox.runner import Runner
    from glassbox.signal.filter import NewsItem

    f, client, workdir = ctx["findings"], ctx["client"], ctx["workdir"]
    structure = ctx["structure"]
    key = structure_key(structure)

    for rnd in range(STORM_ROUNDS):
        sid = f"soak-storm-{ctx['run_id']}-{rnd}"
        coid = client_order_id(sid, key)
        ctx["coids"].add(coid)
        Store(workdir / f"storm-{rnd}.db").close()  # create schema before the stampede
        barrier = threading.Barrier(STORM_THREADS)
        pipeline_calls: list[str] = []
        errors: list[str] = []

        def submit(item, state, sid=sid, coid=coid, rnd=rnd, pipeline_calls=pipeline_calls):
            pipeline_calls.append(threading.current_thread().name)
            tstore = Store(workdir / f"storm-{rnd}.db")
            taudit = AuditLog(workdir / "audit", role=f"storm-{threading.current_thread().name}")
            try:
                router = OrderRouter(trading_client(), tstore, taudit)
                router.submit_structure(structure, 1, RESTING_LIMIT, coid, f"pos-{sid}")
            finally:
                tstore.close()
            return SimpleNamespace(traded=True, reason="soak submit")

        shim = SimpleNamespace(
            _seen_news=set(),
            audit=AuditLog(workdir / "audit", role="storm-shim"),
            trader=SimpleNamespace(process_news=submit),
            market_state=lambda: None,
        )
        item = NewsItem(id=f"news-{sid}", symbol="SPY", headline="soak duplicate storm",
                        summary="", source="soak", created_at=datetime.now(UTC))

        def worker(n, item=item, shim=shim, barrier=barrier, errors=errors):
            barrier.wait()
            try:
                Runner.handle_news(shim, item)
            except Exception as e:  # noqa: BLE001 -- a raise here IS a finding
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(n,), name=f"t{n}")
                   for n in range(STORM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        n_broker = len(broker_orders_for(client, coid))
        f.add("INFO", "news_storm", f"round{rnd}.dedupe",
              f"{len(pipeline_calls)}/{STORM_THREADS} threads passed the _seen_news check "
              f"({'race hit' if len(pipeline_calls) > 1 else 'no race this round'})")
        f.bad(n_broker == 1, "news_storm", f"round{rnd}.broker_orders",
              f"exactly 1 order at broker for {coid[:18]}…",
              f"{n_broker} orders at broker for {coid[:18]}… (expected exactly 1)",
              severity="CRITICAL")
        f.bad(not errors, "news_storm", f"round{rnd}.handle_news_raised",
              "handle_news never raised (errors were swallowed and audited)",
              f"handle_news RAISED out of its guard: {errors[:3]}")

        # A second submitter that lost the race may have clobbered the winner's
        # order row to 'rejected' — status and broker must agree.
        srow = Store(workdir / f"storm-{rnd}.db")
        try:
            row = srow.get_order(coid)
            status = row["status"] if row else "<missing>"
            live = [o for o in broker_orders_for(client, coid)
                    if str(o.status).split(".")[-1].lower() in
                    ("new", "accepted", "pending_new", "held")]
            f.bad(not (live and status == "rejected"), "news_storm",
                  f"round{rnd}.status_agrees",
                  f"store status '{status}' consistent with broker",
                  f"store says '{status}' but the broker holds a LIVE order — "
                  "the losing submitter clobbered the winner's row")
        finally:
            srow.close()

    # Direct same-coid submit storm: even if dedupe swallowed the upstream race,
    # the router-level idempotency must hold under simultaneous fire.
    sid = f"soak-direct-{ctx['run_id']}"
    coid = client_order_id(sid, key)
    ctx["coids"].add(coid)
    Store(workdir / "storm-direct.db").close()  # create schema before the stampede
    barrier = threading.Barrier(STORM_THREADS)
    outcomes: list[str] = []

    def direct(n):
        tstore = Store(workdir / "storm-direct.db")
        taudit = AuditLog(workdir / "audit", role=f"direct-t{n}")
        router = OrderRouter(trading_client(), tstore, taudit)
        barrier.wait()
        try:
            router.submit_structure(structure, 1, RESTING_LIMIT, coid, f"pos-{sid}")
            outcomes.append("submitted-or-suppressed")
        except Exception as e:  # noqa: BLE001 -- expected: broker rejects duplicates
            outcomes.append(f"raised {type(e).__name__}")
        finally:
            tstore.close()

    threads = [threading.Thread(target=direct, args=(n,)) for n in range(STORM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    n_broker = len(broker_orders_for(client, coid))
    f.bad(n_broker == 1, "news_storm", "direct.broker_orders",
          f"{STORM_THREADS} simultaneous submits, exactly 1 broker order",
          f"{STORM_THREADS} simultaneous submits produced {n_broker} broker orders",
          severity="CRITICAL")
    f.add("INFO", "news_storm", "direct.outcomes", str(sorted(set(outcomes))))


# --------------------------------------------------------------------------
# scenario: sqlite
# --------------------------------------------------------------------------

def scenario_sqlite(ctx) -> None:
    import sqlite3

    f, workdir = ctx["findings"], ctx["workdir"]
    db = workdir / "contention.db"
    Store(db).close()  # schema exists before writers race, as in production
    n_threads, n_ops = 8, 150
    errors: list[str] = []
    done = [0] * n_threads

    def writer(n):
        store = Store(db)  # own connection: the multi-process pattern
        try:
            for i in range(n_ops):
                try:
                    kind = i % 5
                    if kind == 0:
                        store.record_order(f"gbx-o-soak{n}-{i}", "open",
                                           [{"symbol": "SPY", "side": "short"}], 1.0, f"pos-{n}-{i}")
                    elif kind == 1:
                        store.upsert_position(f"pos-{n}-{i}", underlying="SPY", kind="soak",
                                              legs_json="[]", qty=1, max_loss=100.0,
                                              status="opening", opened_at=datetime.now(UTC).isoformat())
                    elif kind == 2:
                        store.set_state(f"soak_key_{n}", str(i))
                        store.set_state("trader_heartbeat", datetime.now(UTC).isoformat())
                    elif kind == 3:
                        store.total_heat()
                        store.orders_in_flight()
                    else:
                        with store.tx():
                            store.update_order(f"gbx-o-soak{n}-{max(0, i - 5)}", status="submitted")
                    done[n] += 1
                except Exception as e:  # noqa: BLE001 -- the exception IS the datum
                    errors.append(f"writer{n} op{i}: {type(e).__name__}: {e}")
        finally:
            store.close()

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(n_threads)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    f.add("INFO", "sqlite", "throughput",
          f"{sum(done)}/{n_threads * n_ops} ops across {n_threads} connections "
          f"in {time.time() - t0:.1f}s")
    locked = [e for e in errors if "locked" in e or "busy" in e]
    f.bad(not errors, "sqlite", "separate_connections",
          "no errors under 8-writer contention",
          f"{len(errors)} errors ({len(locked)} lock-related), e.g. {errors[:3]}")

    # The trader's own pattern: ONE Store shared with the stream thread.
    shared = Store(workdir / "shared.db")
    cross_errors: list[str] = []

    def stream_thread():
        try:
            shared.set_state("from_stream_thread", "x")  # what handle_news does
        except Exception as e:  # noqa: BLE001
            cross_errors.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=stream_thread)
    t.start()
    t.join(timeout=10)
    shared.close()
    if cross_errors:
        f.add("CRITICAL", "sqlite", "shared_connection_cross_thread",
              "the trader shares ONE Store between its main loop and the news "
              f"stream thread; cross-thread use raises: {cross_errors[0]} — every "
              "socket-delivered story will error in process_news, be marked seen, "
              "and then be SKIPPED by the poller: socket news is silently lost")
    else:
        f.ok("sqlite", "shared_connection_cross_thread",
             "shared Store usable across threads on this build")

    con = sqlite3.connect(db)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    orders_n = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    con.close()
    f.bad(integrity == "ok", "sqlite", "integrity",
          f"integrity ok, {orders_n} order rows persisted",
          f"PRAGMA integrity_check: {integrity}", severity="CRITICAL")


# --------------------------------------------------------------------------
# scenario: kill
# --------------------------------------------------------------------------

def scenario_kill(ctx) -> None:
    from glassbox.supervisor.guards import KILL_SWITCH_FILE, evaluate_guards
    from glassbox.supervisor.run import kill_switch_engaged
    from glassbox.supervisor.run import tick as supervisor_tick

    f, client, workdir, cfg = ctx["findings"], ctx["client"], ctx["workdir"], ctx["cfg"]
    kill_file = ROOT / KILL_SWITCH_FILE
    kill_file.parent.mkdir(parents=True, exist_ok=True)
    if kill_file.exists():
        f.add("FAIL", "kill", "precondition",
              f"{KILL_SWITCH_FILE} already exists — clear it (`make resume`) and rerun")
        return
    if _glassbox_processes():
        f.add("FAIL", "kill", "precondition",
              f"live glassbox processes running: {_glassbox_processes()} — "
              "refusing to flip the real kill switch under them")
        return

    structure = ctx["structure"]
    key = structure_key(structure)
    store = Store(workdir / "kill.db")
    audit = AuditLog(workdir / "audit", role="kill-soak")
    router = OrderRouter(client, store, audit)
    try:
        sid = f"soak-kill-{ctx['run_id']}"
        coid = client_order_id(sid, key)
        ctx["coids"].add(coid)
        router.submit_structure(structure, 1, RESTING_LIMIT, coid, f"pos-{sid}")

        kill_file.touch()  # operator slams the button while the order rests
        try:
            f.bad(kill_switch_engaged(ROOT), "kill", "engaged_detected",
                  "kill_switch_engaged() sees the file", "kill file NOT detected",
                  severity="CRITICAL")
            verdict = evaluate_guards(equity=100_000, session_start_equity=100_000,
                                      peak_equity=100_000, cfg=cfg, kill_switch=True)
            f.bad(str(verdict.action) == "halt_hard" and verdict.should_flatten,
                  "kill", "guard_verdict",
                  "evaluate_guards → HALT_HARD + flatten",
                  f"unexpected verdict {verdict.action}", severity="CRITICAL")
            action = supervisor_tick(store, audit, client, cfg, ROOT, dry_run=True)
            f.bad(str(action) == "halt_hard", "kill", "supervisor_tick",
                  "real supervisor tick returned HALT_HARD (dry-run, no flatten)",
                  f"supervisor tick returned {action}", severity="CRITICAL")
            f.bad(bool(store.get_state("halt_reason")), "kill", "halt_persisted",
                  f"halt latched: {store.get_state('halt_reason')!r}",
                  "halt_reason was not persisted")
            still_live = [o for o in broker_orders_for(client, coid)
                          if str(o.status).split(".")[-1].lower() in
                          ("new", "accepted", "pending_new", "held")]
            f.bad(len(still_live) == 1, "kill", "dry_run_left_order",
                  "dry-run tick left the resting order untouched (flatten is live-only)",
                  f"resting order state changed under dry-run: {len(still_live)} live")
        finally:
            kill_file.unlink(missing_ok=True)

        action = supervisor_tick(store, audit, client, cfg, ROOT, dry_run=True)
        f.bad(str(action) == "continue" and bool(store.get_state("halt_reason")),
              "kill", "halt_latch_after_clear",
              "kill cleared → CONTINUE, but halt stays latched pending manual reset",
              f"after clearing: action={action}, halt={store.get_state('halt_reason')!r}")

        # Rapid flips while a stream of submits is in flight: the router must
        # stay consistent (entry *blocking* is the gate's job, not the router's).
        stop = threading.Event()

        def flipper():
            while not stop.is_set():
                kill_file.touch()
                time.sleep(0.04)
                kill_file.unlink(missing_ok=True)
                time.sleep(0.04)

        flip = threading.Thread(target=flipper)
        flip.start()
        try:
            mid_coids = []
            for i in range(5):
                sid_i = f"soak-killmid-{ctx['run_id']}-{i}"
                coid_i = client_order_id(sid_i, key)
                mid_coids.append(coid_i)
                ctx["coids"].add(coid_i)
                router.submit_structure(structure, 1, RESTING_LIMIT, coid_i, f"pos-{sid_i}")
        finally:
            stop.set()
            flip.join(timeout=5)
            kill_file.unlink(missing_ok=True)
        counts = {c: len(broker_orders_for(client, c)) for c in mid_coids}
        f.bad(all(v == 1 for v in counts.values()), "kill", "mid_flip_submissions",
              "5 submits during rapid kill flips: one broker order each",
              f"unexpected broker order counts: {counts}", severity="CRITICAL")

        for c in [coid, *mid_coids]:
            row = store.get_order(c)
            if row and row["alpaca_order_id"]:
                with contextlib.suppress(Exception):
                    router.cancel(c, row["alpaca_order_id"])
    finally:
        kill_file.unlink(missing_ok=True)
        store.close()


# --------------------------------------------------------------------------
# scenario: crash
# --------------------------------------------------------------------------

CRASH_HELPER = '''\
import os, sys, time
sys.path.insert(0, {root!r})
os.chdir({root!r})
from glassbox.audit import AuditLog
from glassbox.data.alpaca_client import trading_client
from glassbox.execution.router import OrderRouter
from glassbox.store import Store
from glassbox.verify_order import build_far_otm_put_spread

coid, db, auditdir = sys.argv[1], sys.argv[2], sys.argv[3]
client = trading_client()
store = Store(db)
router = OrderRouter(client, store, AuditLog(auditdir, role="crash-child"))
structure = build_far_otm_put_spread(client)
print("READY", flush=True)
router.submit_structure(structure, 1, {limit}, coid, "pos-" + coid[-8:])
print("SUBMITTED", flush=True)
time.sleep(60)  # hold until the parent SIGKILLs us
'''


def scenario_crash(ctx) -> None:
    f, client, workdir = ctx["findings"], ctx["client"], ctx["workdir"]
    structure = ctx["structure"]
    key = structure_key(structure)
    helper = workdir / "crash_child.py"
    helper.write_text(CRASH_HELPER.format(root=str(ROOT), limit=RESTING_LIMIT))
    db = workdir / "crash.db"

    for trial, delay in enumerate([0.05, 0.4, 2.5]):  # before, during, after submit
        sid = f"soak-crash-{ctx['run_id']}-{trial}"
        coid = client_order_id(sid, key)
        ctx["coids"].add(coid)
        proc = subprocess.Popen(
            [sys.executable, str(helper), coid, str(db), str(workdir / "audit")],
            cwd=ROOT, stdout=subprocess.PIPE, text=True,
        )
        line = proc.stdout.readline().strip()  # wait for READY so timing is real
        time.sleep(delay + random.uniform(0, 0.1))
        proc.kill()
        proc.wait(timeout=10)
        f.add("INFO", "crash", f"trial{trial}.killed",
              f"SIGKILL {delay}s after {line or 'start'}")

        # Restart: rebuild exactly the way a fresh process would — read our own
        # in-flight intents, ask the broker what became of each, converge.
        store = Store(db)
        try:
            resolved = []
            for row in store.orders_in_flight():
                c = row["client_order_id"]
                try:
                    order = client.get_order_by_client_id(c)
                    store.update_order(c, status="submitted", alpaca_order_id=str(order.id))
                    resolved.append(f"{c[-8:]}=at-broker")
                except Exception:  # noqa: BLE001 -- broker never saw it
                    store.update_order(c, status="canceled")
                    resolved.append(f"{c[-8:]}=never-arrived")
            f.add("INFO", "crash", f"trial{trial}.rebuild", ", ".join(resolved) or "no in-flight rows")

            # The idempotent retry: resubmitting after the rebuild must converge
            # on exactly one broker order whichever side of the crash we landed.
            audit = AuditLog(workdir / "audit", role="crash-restart")
            router = OrderRouter(client, store, audit)
            router.submit_structure(structure, 1, RESTING_LIMIT, coid, f"pos-{sid}")
            n_broker = len(broker_orders_for(client, coid))
            f.bad(n_broker == 1, "crash", f"trial{trial}.converged",
                  "restart + idempotent retry → exactly 1 broker order",
                  f"{n_broker} broker orders after restart retry", severity="CRITICAL")

            rec = reconcile(store, client.get_all_positions())
            f.bad(not (rec.local_only or rec.qty_mismatch), "crash",
                  f"trial{trial}.reconcile",
                  f"reconcile clean for our state ({rec.reason})",
                  f"reconcile divergence after rebuild: {rec.reason}")

            row = store.get_order(coid)
            if row and row["alpaca_order_id"]:
                with contextlib.suppress(Exception):
                    router.cancel(coid, row["alpaca_order_id"])
        finally:
            store.close()


# --------------------------------------------------------------------------
# scenario: scheduler
# --------------------------------------------------------------------------

def scenario_scheduler(ctx) -> None:
    import sqlite3

    from glassbox.dashboard.audit_reader import read_records
    from glassbox.report.generate import collect

    f, workdir = ctx["findings"], ctx["workdir"]
    db = workdir / "collide.db"
    Store(db).close()  # schema exists before writers race, as in production
    auditdir = workdir / "collide-audit"
    stop = threading.Event()
    errors: list[str] = []
    DURATION = 12.0

    def role_loop(role: str, body):
        store = Store(db)
        audit = AuditLog(auditdir, role=role)
        try:
            i = 0
            while not stop.is_set():
                try:
                    body(store, audit, i)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{role}: {type(e).__name__}: {e}")
                i += 1
                time.sleep(0.01)
        finally:
            store.close()

    def trader_body(store, audit, i):
        store.set_state("trader_heartbeat", datetime.now(UTC).isoformat())
        store.upsert_position(f"pos-col-{i % 20}", underlying="SPY", kind="soak",
                              legs_json="[]", qty=1, max_loss=50.0, status="open",
                              opened_at=datetime.now(UTC).isoformat())
        audit.append("tick", {"i": i})

    def scheduler_body(store, audit, i):
        store.record_prediction(f"pred-{i}", symbol="SPY",
                                predicted_at=datetime.now(UTC).isoformat(),
                                spot_at_prediction=500.0, expected_move_pct=1.0,
                                direction="up", horizon_hours=4.0,
                                resolve_after=datetime.now(UTC).isoformat())
        store.resolve_prediction(f"pred-{max(0, i - 3)}", 1.0, 1.0)
        audit.append("job_start", {"job": "soak", "i": i})

    def supervisor_body(store, audit, i):
        store.set_state("peak_equity", "100000")
        store.get_state("trader_heartbeat")
        store.total_heat()
        audit.append("supervisor_tick", {"i": i})

    def reader_body(store, audit, i):
        collect(auditdir)  # the nightly report reading while everyone writes
        read_records(auditdir, days=1)
        verify_day(auditdir)

    threads = [threading.Thread(target=role_loop, args=(role, body)) for role, body in [
        ("trader", trader_body), ("scheduler", scheduler_body),
        ("supervisor", supervisor_body), ("reader", reader_body),
    ]]
    for t in threads:
        t.start()
    time.sleep(DURATION)
    stop.set()
    for t in threads:
        t.join(timeout=30)

    f.bad(not errors, "scheduler", "no_collision_errors",
          f"trader/scheduler/supervisor/reader ran {DURATION:.0f}s with zero errors",
          f"{len(errors)} errors, e.g. {errors[:3]}")
    ok, n, broken = verify_day(auditdir)
    f.bad(ok and n > 0, "scheduler", "audit_chains_verify",
          f"all per-role chains valid after collision ({n} records)",
          f"chains broken under collision: {broken}", severity="CRITICAL")
    con = sqlite3.connect(db)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    f.bad(integrity == "ok", "scheduler", "db_integrity",
          "database intact", f"integrity_check: {integrity}", severity="CRITICAL")


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

SCENARIOS = {
    "news_storm": (scenario_news_storm, True),
    "sqlite": (scenario_sqlite, False),
    "kill": (scenario_kill, True),
    "crash": (scenario_crash, True),
    "scheduler": (scenario_scheduler, False),
}


def _in_container(pid: str) -> bool:
    """Whether a PID belongs to a container rather than this host tree.

    Container processes are visible to the host's pgrep, but a containerised
    trader reads its own mounted volume — it cannot see this run's kill file
    or database, so it is not something we are about to flip a switch under.
    Only meaningful on Linux; on macOS the engine runs in a VM and its
    processes never appear here at all, so the answer is correctly False.
    """
    try:
        return "docker" in Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return False


def _glassbox_processes() -> list[str]:
    """Live glassbox processes that share THIS directory's state.

    The precondition it serves is "do not flip the real kill switch under a
    running trader". A trader in a container, or one started from a different
    checkout, reads a different data/ — so neither is a reason to refuse, and
    treating them as one makes the soak unrunnable alongside a live stack.
    """
    out = subprocess.run(
        ["pgrep", "-fl", "glassbox.runner|glassbox.supervisor|glassbox.scheduler"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    mine = str(os.getpid())
    here = ROOT.resolve()
    live = []
    for line in out.splitlines():
        if not line or line.startswith(mine):
            continue
        pid = line.split(None, 1)[0]
        if _in_container(pid):
            continue
        # A host process counts only if its working directory is ours, since
        # that is what decides whether it reads our data/ and our KILL file.
        try:
            if Path(f"/proc/{pid}/cwd").resolve() != here:
                continue
        except OSError:
            pass  # cwd unreadable (macOS, or a foreign owner): assume it counts
        live.append(line)
    return live


def main() -> int:
    parser = argparse.ArgumentParser(description="GlassBox active soak (dev account only)")
    parser.add_argument("--scenario", default="all", help="|".join([*SCENARIOS, "all"]))
    parser.add_argument(
        "--account",
        required=True,
        help="Alpaca paper account number this soak is allowed to touch; "
        "the run aborts if the credentials in .env resolve to any other account",
    )
    parser.add_argument("--allow-open", action="store_true",
                        help="run broker scenarios even while the market is open")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for n in names:
        if n not in SCENARIOS:
            print(f"unknown scenario {n}; known: {list(SCENARIOS)}")
            return 2

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    workdir = ROOT / "soak-results" / f"active-{run_id}"
    (workdir / "audit").mkdir(parents=True, exist_ok=True)
    findings = Findings(workdir / "findings.jsonl")
    print(f"ACTIVE SOAK {run_id} — workdir {workdir}")

    needs_broker = any(SCENARIOS[n][1] for n in names)
    client = None
    ctx = {
        "findings": findings, "workdir": workdir, "run_id": run_id,
        "cfg": load_config(), "coids": set(), "client": None, "structure": None,
    }
    if needs_broker:
        client = trading_client()
        acct = client.get_account()
        if acct.account_number != args.account:
            print(f"REFUSING: account {acct.account_number} != expected {args.account}. "
                  "This soak runs on the dev account only.")
            return 2
        print(f"account {acct.account_number} (dev)  equity=${acct.equity}")
        clock = client.get_clock()
        if clock.is_open and not args.allow_open:
            print("REFUSING: market is OPEN. The active soak is a pre-open drill "
                  "(--allow-open to override, but do not do this during the live session).")
            return 2
        ctx["client"] = client
        ctx["structure"] = build_far_otm_put_spread(client)
        print(f"resting structure: {structure_key(ctx['structure'])}")

    t0 = time.time()
    try:
        for name in names:
            fn, _ = SCENARIOS[name]
            print(f"\n=== {name} ===")
            try:
                fn(ctx)
            except Exception as e:  # noqa: BLE001 -- a scenario crash is a finding,
                # not a reason to skip the cleanup sweep below.
                findings.add("FAIL", name, "scenario_crashed", f"{type(e).__name__}: {e}")
    finally:
        from glassbox.supervisor.guards import KILL_SWITCH_FILE

        (ROOT / KILL_SWITCH_FILE).unlink(missing_ok=True)
        (ROOT / "KILL").unlink(missing_ok=True)  # legacy root-level sentinel
        if client is not None and ctx["coids"]:
            print("\n=== cleanup ===")
            cancel_soak_orders(client, ctx["coids"], findings)

    by_sev: dict[str, int] = {}
    for r in findings.rows:
        by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + 1
    print(f"\nSOAK {'FAILED' if findings.failed else 'PASSED'} in {time.time() - t0:.0f}s — "
          + ", ".join(f"{k}:{v}" for k, v in sorted(by_sev.items())))
    print(f"findings: {findings.path}")
    print("now run: make drill-clean   (required before the 9:30 drills)")
    return 1 if findings.failed else 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(main())
