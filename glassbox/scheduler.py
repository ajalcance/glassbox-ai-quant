"""Scheduled jobs — the nightly report and the model refit.

A small in-process scheduler rather than cron. Two reasons: cron inside a
container is a known source of silent failure (no environment, no logs, wrong
user), and every time in this system is a *market* time. The operator is in
Philippine time and the container clock is UTC, so a job expressed as "16:15"
in either of those is wrong. Jobs are declared in US Eastern and resolved
through the same clock module the trader uses.

Each job runs at most once per market day, tracked in the store, so a restart at
20:00 does not re-run a job that already fired at 16:15.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from glassbox.audit import AuditLog
from glassbox.clock import MARKET_TZ, now_market
from glassbox.config import load_config
from glassbox.store import Store

LAST_RUN_PREFIX = "job_last_run:"


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    hour: int  # US Eastern
    minute: int
    run: Callable[[], int]
    weekdays_only: bool = True

    def due_at(self, day: date) -> datetime:
        return datetime(day.year, day.month, day.day, self.hour, self.minute, tzinfo=MARKET_TZ)


def _run_module(*args: str) -> int:
    """Run a module in a subprocess so a crash in a job cannot take the
    scheduler down with it."""
    proc = subprocess.run(
        [sys.executable, "-m", *args], capture_output=True, text=True, timeout=600, check=False
    )
    if proc.stdout:
        print(proc.stdout.rstrip()[:4000])
    if proc.returncode != 0 and proc.stderr:
        print(proc.stderr.rstrip()[:1000], file=sys.stderr)
    return proc.returncode


def _resolve_predictions() -> int:
    """Score analyst estimates whose horizon has elapsed."""
    from pathlib import Path

    from glassbox import predictions
    from glassbox.data.alpaca_client import (
        option_data_client,
        stock_data_client,
        trading_client,
    )
    from glassbox.data.market import MarketData

    cfg = load_config()
    store = Store(cfg.paths.db)
    try:
        data = MarketData(
            trading_client=trading_client(),
            stock_client=stock_data_client(),
            option_client=option_data_client(),
            store=store,
            root=Path("."),
        )
        n = predictions.resolve_due(store, data)
        calib = predictions.calibration(store)
        print(f"scored {n} prediction(s)")
        if calib:
            print(f"  calibration: {calib.as_dict()}")
        return 0
    finally:
        store.close()


JOBS: tuple[Job, ...] = (
    # Before the report, so the day's summary reflects estimates already scored.
    Job("resolve_predictions", 16, 10, _resolve_predictions),
    # Fifteen minutes after the close, so late fills are settled before the day
    # is summarised.
    Job("nightly_report", 16, 15, lambda: _run_module("glassbox.report.run")),
    # Refit after the report, so the report describes the model that traded the
    # day rather than the one built from it.
    Job("model_refit", 16, 25, lambda: _run_module("glassbox.ml.train", "--meta-only")),
)


def already_ran(store, job: Job, day: date) -> bool:
    return store.get_state(f"{LAST_RUN_PREFIX}{job.name}") == day.isoformat()


def mark_ran(store, job: Job, day: date) -> None:
    store.set_state(f"{LAST_RUN_PREFIX}{job.name}", day.isoformat())


def due_jobs(store, now: datetime, jobs=JOBS) -> list[Job]:
    """Jobs whose time has passed today and which have not run yet."""
    day = now.date()
    out = []
    for job in jobs:
        if job.weekdays_only and day.weekday() >= 5:
            continue
        if now >= job.due_at(day) and not already_ran(store, job, day):
            out.append(job)
    return out


def tick(store, audit: AuditLog, now: datetime | None = None, jobs=JOBS) -> list[str]:
    now = now or now_market()
    ran = []
    for job in due_jobs(store, now, jobs):
        audit.append("job_start", {"job": job.name, "market_time": now.isoformat()})
        try:
            code = job.run()
        except Exception as e:  # noqa: BLE001 -- one failed job must not stop the
            # others, and must not stop tomorrow's run either.
            audit.append("job_error", {"job": job.name, "error": f"{type(e).__name__}: {e}"})
            print(f"job {job.name} failed: {type(e).__name__}: {e}", file=sys.stderr)
            code = 1
        # Marked regardless of outcome: a job that fails should be investigated,
        # not retried in a loop every sixty seconds until it succeeds.
        mark_ran(store, job, now.date())
        audit.append("job_done", {"job": job.name, "exit_code": code})
        ran.append(job.name)
    return ran


def main() -> int:
    parser = argparse.ArgumentParser(description="GlassBox scheduled jobs")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true", help="run due jobs then exit")
    parser.add_argument("--force", metavar="JOB", help="run one job now, ignoring its schedule")
    args = parser.parse_args()

    cfg = load_config()
    store = Store(cfg.paths.db)
    audit = AuditLog(cfg.paths.audit_dir, role="scheduler")

    try:
        if args.force:
            job = next((j for j in JOBS if j.name == args.force), None)
            if job is None:
                print(f"unknown job {args.force}; known: {[j.name for j in JOBS]}")
                return 1
            print(f"forcing {job.name}")
            return job.run()

        if args.once:
            ran = tick(store, audit)
            print(f"ran: {ran or 'nothing due'}")
            return 0

        schedule = ", ".join(f"{j.name} at {j.hour:02d}:{j.minute:02d} ET" for j in JOBS)
        print(f"scheduler running — {schedule}")
        while True:
            now = now_market()
            for name in tick(store, audit, now):
                print(f"[{now:%H:%M %Z}] ran {name}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
