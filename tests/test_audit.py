import hashlib
import json

from glassbox.audit import GENESIS, AuditLog


def _day_file(log: AuditLog):
    files = sorted(log.dir.glob("*.jsonl"))
    assert files, "no audit file written"
    return files[-1]


def test_chain_starts_at_genesis_and_links(tmp_path):
    log = AuditLog(tmp_path)
    r1 = log.append("signal", {"symbol": "SPY"})
    r2 = log.append("gate", {"verdict": "VETO"})
    assert r1["prev_hash"] == GENESIS
    line1 = _day_file(log).read_bytes().splitlines()[0]
    assert r2["prev_hash"] == hashlib.sha256(line1).hexdigest()


def test_verify_chain_ok(tmp_path):
    log = AuditLog(tmp_path)
    for i in range(10):
        log.append("order", {"i": i})
    ok, n = AuditLog.verify_chain(_day_file(log))
    assert ok and n == 10


def test_tamper_detected(tmp_path):
    log = AuditLog(tmp_path)
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
    log1 = AuditLog(tmp_path)
    log1.append("halt", {"reason": "test"})
    log2 = AuditLog(tmp_path)  # simulates process restart
    log2.append("resume", {})
    ok, n = AuditLog.verify_chain(_day_file(log2))
    assert ok and n == 2
