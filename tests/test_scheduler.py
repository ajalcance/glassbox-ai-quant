"""Scheduler tests.

Every time in this system is a market time. The operator is in Philippine time
(UTC+8) and containers run UTC, so a job that fires on the wrong day or hour is
a realistic failure — these tests pin that down.
"""

from datetime import date, datetime

from glassbox.clock import MARKET_TZ
from glassbox.scheduler import Job, already_ran, due_jobs, mark_ran, tick


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=MARKET_TZ)


def make_job(name="test_job", hour=16, minute=15, result=0, weekdays_only=True):
    calls = []

    def run():
        calls.append(1)
        return result

    return Job(name, hour, minute, run, weekdays_only), calls


# --- scheduling ------------------------------------------------------------


def test_job_not_due_before_its_time(store):
    job, _ = make_job()
    assert due_jobs(store, at(2026, 9, 1, 15, 59), [job]) == []


def test_job_due_at_and_after_its_time(store):
    job, _ = make_job()
    assert due_jobs(store, at(2026, 9, 1, 16, 15), [job]) == [job]
    assert due_jobs(store, at(2026, 9, 1, 20, 0), [job]) == [job]


def test_job_runs_once_per_day(store):
    """A restart at 20:00 must not re-run a job that already fired at 16:15."""
    job, _ = make_job()
    day = date(2026, 9, 1)
    assert not already_ran(store, job, day)
    mark_ran(store, job, day)
    assert already_ran(store, job, day)
    assert due_jobs(store, at(2026, 9, 1, 20, 0), [job]) == []


def test_job_becomes_due_again_the_next_day(store):
    job, _ = make_job()
    mark_ran(store, job, date(2026, 9, 1))
    assert due_jobs(store, at(2026, 9, 2, 16, 15), [job]) == [job]


def test_weekend_is_skipped(store):
    job, _ = make_job()
    assert due_jobs(store, at(2026, 9, 5, 17, 0), [job]) == []  # Saturday
    assert due_jobs(store, at(2026, 9, 6, 17, 0), [job]) == []  # Sunday
    assert due_jobs(store, at(2026, 9, 4, 17, 0), [job]) == [job]  # Friday


def test_schedule_is_eastern_not_local(store):
    """16:15 ET is 04:15 the next day in Philippine time. A job expressed in the
    operator's own timezone would fire on the wrong day."""
    job, _ = make_job(hour=16, minute=15)
    due = job.due_at(date(2026, 9, 1))
    assert due.tzinfo is MARKET_TZ
    assert due.hour == 16
    manila = due.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Manila"))
    assert manila.day == 2 and manila.hour == 4, "confirms the day rolls over locally"


# --- execution -------------------------------------------------------------


def test_tick_runs_a_due_job_once(store, audit):
    job, calls = make_job()
    now = at(2026, 9, 1, 16, 30)
    assert tick(store, audit, now, [job]) == ["test_job"]
    assert tick(store, audit, now, [job]) == [], "must not run twice in a day"
    assert len(calls) == 1


def test_failing_job_is_recorded_and_not_retried_in_a_loop(store, audit):
    """A job that fails should be investigated, not retried every 60 seconds."""

    def explode():
        raise RuntimeError("provider down")

    job = Job("bad_job", 16, 15, explode)
    now = at(2026, 9, 1, 16, 30)
    assert tick(store, audit, now, [job]) == ["bad_job"]
    assert tick(store, audit, now, [job]) == []


def test_one_failing_job_does_not_block_the_others(store, audit):
    def explode():
        raise RuntimeError("boom")

    bad = Job("bad", 16, 15, explode)
    good, calls = make_job(name="good", hour=16, minute=16)
    ran = tick(store, audit, at(2026, 9, 1, 17, 0), [bad, good])
    assert ran == ["bad", "good"] and len(calls) == 1


def test_job_outcomes_are_audited(store, audit, tmp_path):
    import json

    job, _ = make_job()
    tick(store, audit, at(2026, 9, 1, 16, 30), [job])
    path = next(iter(sorted(audit.dir.glob("*.jsonl"))))
    kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
    assert "job_start" in kinds and "job_done" in kinds


def test_real_jobs_are_ordered_report_then_refit():
    """The report must describe the model that traded the day, not the one
    rebuilt from it."""
    from glassbox.scheduler import JOBS

    names = [j.name for j in JOBS]
    assert names.index("nightly_report") < names.index("model_refit")
    report = next(j for j in JOBS if j.name == "nightly_report")
    refit = next(j for j in JOBS if j.name == "model_refit")
    assert (report.hour, report.minute) < (refit.hour, refit.minute)
    assert report.hour >= 16, "must run after the close"
