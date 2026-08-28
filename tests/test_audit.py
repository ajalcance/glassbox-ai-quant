import hashlib
import json
from datetime import UTC, datetime

import pytest

from glassbox.audit import GENESIS, AuditLog, day_files, verify_day


def _day_file(log: AuditLog):
    files = sorted(log.dir.glob("*.jsonl"))
    assert files, "no audit file written"
    return files[-1]


def test_chain_starts_at_genesis_and_links(tmp_path):
    log = AuditLog(tmp_path, role="trader")
    r1 = log.append("signal", {"symbol": "SPY"})
    r2 = log.append("gate", {"verdict": "VETO"})
    assert r1["prev_hash"] == GENESIS
    line1 = _day_file(log).read_bytes().splitlines()[0]
    assert r2["prev_hash"] == hashlib.sha256(line1).hexdigest()


def test_verify_chain_ok(tmp_path):
    log = AuditLog(tmp_path, role="trader")
    for i in range(10):
        log.append("order", {"i": i})
    ok, n = AuditLog.verify_chain(_day_file(log))
    assert ok and n == 10


def test_tamper_detected(tmp_path):
    log = AuditLog(tmp_path, role="trader")
    for i in range(5):
        log.append("fill", {"qty": i})
    path = _day_file(log)
    lines = path.read_bytes().splitlines()
    rec = json.loads(lines[2])
    rec["qty"] = 999  # tamper with a middle record
    lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(b"\n".join(lines) + b"\n")
    ok, checked = AuditLog.verify_chain(path)
    assert not ok and checked == 3  # fails at the record after the tampered one


def test_resume_after_restart_continues_chain(tmp_path):
    log1 = AuditLog(tmp_path, role="trader")
    log1.append("halt", {"reason": "test"})
    log2 = AuditLog(tmp_path, role="trader")  # simulates process restart
    log2.append("resume", {})
    ok, n = AuditLog.verify_chain(_day_file(log2))
    assert ok and n == 2


def test_concurrent_roles_write_separate_valid_chains(tmp_path):
    """The multi-writer bug: trader, supervisor and scheduler all appending
    while each caches its own prev_hash. Per-role files keep every chain valid
    no matter how the writes interleave."""
    trader = AuditLog(tmp_path, role="trader")
    supervisor = AuditLog(tmp_path, role="supervisor")
    scheduler = AuditLog(tmp_path, role="scheduler")
    for i in range(5):  # interleave the way concurrent processes would
        trader.append("gate", {"i": i})
        supervisor.append("heartbeat", {"i": i})
        scheduler.append("job", {"i": i})
    files = day_files(tmp_path)
    assert len(files) == 3
    for path in files:
        ok, n = AuditLog.verify_chain(path)
        assert ok and n == 5, path.name
    ok, total, broken = verify_day(tmp_path)
    assert ok and total == 15 and broken == []


def test_verify_day_flags_only_the_broken_role(tmp_path):
    good = AuditLog(tmp_path, role="trader")
    bad = AuditLog(tmp_path, role="scheduler")
    good.append("gate", {})
    bad.append("job", {})
    bad.append("job", {})
    path = bad.path_for(datetime.now(UTC))
    lines = path.read_bytes().splitlines()
    rec = json.loads(lines[0])
    rec["job"] = "tampered"
    lines[0] = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(b"\n".join(lines) + b"\n")
    ok, _total, broken = verify_day(tmp_path)
    assert not ok
    assert broken == [path.name]


def test_role_must_be_filename_safe(tmp_path):
    with pytest.raises(ValueError):
        AuditLog(tmp_path, role="../evil")


def test_legacy_undated_role_file_is_still_verified(tmp_path):
    """Pre-split YYYY-MM-DD.jsonl files remain valid single chains and must
    stay covered by verify_day."""
    from glassbox.clock import now_utc

    rec = {"record_id": "r1", "ts": now_utc().isoformat(), "prev_hash": GENESIS, "kind": "gate"}
    line = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()
    (tmp_path / f"{now_utc():%Y-%m-%d}.jsonl").write_bytes(line + b"\n")
    ok, total, broken = verify_day(tmp_path)
    assert ok and total == 1 and broken == []
