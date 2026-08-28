"""Hash-chained, append-only audit log.

One JSONL file per UTC day. Every record embeds the SHA-256 of the previous
record, making the log tamper-evident. Records are never rewritten —
corrections are new records referencing the old record_id.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()


class AuditLog:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._prev_hash: str | None = None  # lazily initialised from today's file

    def _path_for(self, ts: datetime) -> Path:
        return self.dir / f"{ts.strftime('%Y-%m-%d')}.jsonl"

    def _last_hash(self, path: Path) -> str:
        """Hash of the last record in the file, or GENESIS if empty/missing."""
        if not path.exists() or path.stat().st_size == 0:
            return GENESIS
        with open(path, "rb") as f:
            last_line = f.read().splitlines()[-1]
        return hashlib.sha256(last_line).hexdigest()

    def append(self, kind: str, payload: dict) -> dict:
        """Append one record and return it (including its record_id)."""
        ts = datetime.now(timezone.utc)
        path = self._path_for(ts)
        if self._prev_hash is None:
            self._prev_hash = self._last_hash(path)
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
