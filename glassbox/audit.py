"""Hash-chained, append-only audit log.

One JSONL file per UTC day *per writing process*. Every record embeds the
SHA-256 of the previous record in its own file, making each file tamper-evident.
Records are never rewritten — corrections are new records referencing the old
record_id.

The per-process split is what keeps the chains valid: trader, supervisor and
scheduler each cache their own prev_hash, so sharing one file would interleave
records and break every chain under normal concurrency. Each process therefore
owns `YYYY-MM-DD-<role>.jsonl` exclusively, and a day is verified by checking
every role's file independently (verify_day).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

GENESIS = "0" * 64

_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()


class AuditLog:
    def __init__(self, directory: str | Path, role: str):
        if not _ROLE_RE.match(role):
            raise ValueError(f"audit role must be a safe filename component, got {role!r}")
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.role = role
        self._prev_hash: str | None = None  # lazily initialised from today's file
        self._path: Path | None = None

    def path_for(self, ts: datetime) -> Path:
        """This process's own file for the given UTC timestamp's day."""
        return self.dir / f"{ts.strftime('%Y-%m-%d')}-{self.role}.jsonl"

    def _last_hash(self, path: Path) -> str:
        """Hash of the last record in the file, or GENESIS if empty/missing."""
        if not path.exists() or path.stat().st_size == 0:
            return GENESIS
        with open(path, "rb") as f:
            last_line = f.read().splitlines()[-1]
        return hashlib.sha256(last_line).hexdigest()

    def append(self, kind: str, payload: dict) -> dict:
        """Append one record and return it (including its record_id)."""
        ts = datetime.now(UTC)
        path = self.path_for(ts)
        if self._prev_hash is None or path != self._path:
            # First write, or the UTC day rolled over: each day-file is its own
            # chain, so the cached hash from yesterday's file must not leak in.
            self._prev_hash = self._last_hash(path)
            self._path = path
        record = {
            "record_id": uuid.uuid4().hex,
            "ts": ts.isoformat(),
            "prev_hash": self._prev_hash,
            "kind": kind,
            **payload,
        }
        line = _canonical(record)
        with open(path, "ab") as f:
            f.write(line + b"\n")
        self._prev_hash = hashlib.sha256(line).hexdigest()
        return record

    @staticmethod
    def verify_chain(path: str | Path) -> tuple[bool, int]:
        """Verify a day-file's hash chain. Returns (ok, records_checked)."""
        path = Path(path)
        prev = GENESIS
        count = 0
        with open(path, "rb") as f:
            for line in f.read().splitlines():
                record = json.loads(line)
                if record["prev_hash"] != prev:
                    return False, count
                if _canonical(record) != line:
                    return False, count  # non-canonical line => tampered
                prev = hashlib.sha256(line).hexdigest()
                count += 1
        return True, count


def day_files(directory: str | Path, day: date | None = None) -> list[Path]:
    """Every audit file for one UTC day, all roles — including legacy
    pre-split `YYYY-MM-DD.jsonl` files, which remain valid single chains."""
    if day is None:
        day = datetime.now(UTC).date()
    return sorted(Path(directory).glob(f"{day:%Y-%m-%d}*.jsonl"))


def verify_day(directory: str | Path, day: date | None = None) -> tuple[bool, int, list[str]]:
    """Verify every role's chain for one UTC day, each independently.

    Returns (all_ok, total_records, names_of_broken_files).
    """
    total = 0
    broken: list[str] = []
    for path in day_files(directory, day):
        ok, n = AuditLog.verify_chain(path)
        total += n
        if not ok:
            broken.append(path.name)
    return not broken, total, broken
