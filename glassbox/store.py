"""SQLite state store. The broker is truth; this is a cache and an audit aid.

All SQL lives here — business logic never writes raw SQL.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    alpaca_order_id TEXT,
    position_id     TEXT,
    intent          TEXT NOT NULL,      -- open | close
    status          TEXT NOT NULL,      -- pending | submitted | filled | canceled | rejected
    legs_json       TEXT NOT NULL,
    limit_price     REAL,
    filled_price    REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    position_id   TEXT PRIMARY KEY,
    signal_id     TEXT,
    underlying    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    legs_json     TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    entry_price   REAL,
    max_loss      REAL NOT NULL,
    status        TEXT NOT NULL,        -- opening | open | closing | closed
    horizon_hours REAL,
    peak_pnl      REAL NOT NULL DEFAULT 0,
    exit_barrier  TEXT,
    meta_label    INTEGER,
    opened_at     TEXT,
    closed_at     TEXT,
    exit_reason   TEXT,
    realized_pnl  REAL
);

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_position ON orders(position_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently leaves an older table alone, so a
        store built by a previous version would be missing newer columns and
        fail at write time — mid-session, holding real positions.
        """
        wanted = {
            "positions": {
                "horizon_hours": "REAL",
                "peak_pnl": "REAL NOT NULL DEFAULT 0",
                "exit_barrier": "TEXT",
                "meta_label": "INTEGER",
            }
        }
        for table, columns in wanted.items():
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---- orders -----------------------------------------------------------
    def record_order(
        self,
        client_order_id: str,
        intent: str,
        legs: list[dict],
        limit_price: float | None,
        position_id: str | None = None,
    ) -> None:
        """Write the order intent BEFORE submitting, so a crash mid-submit
        leaves a record to reconcile against."""
        self._conn.execute(
            "INSERT OR IGNORE INTO orders "
            "(client_order_id, position_id, intent, status, legs_json, limit_price,"
            " created_at, updated_at) VALUES (?,?,?,'pending',?,?,?,?)",
            (client_order_id, position_id, intent, json.dumps(legs), limit_price, _now(), _now()),
        )

    def update_order(self, client_order_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(
            f"UPDATE orders SET {sets} WHERE client_order_id=?",
            (*fields.values(), client_order_id),
        )

    def get_order(self, client_order_id: str) -> sqlite3.Row | None:
        cur = self._conn.execute("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,))
        return cur.fetchone()

    def orders_in_flight(self) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM orders WHERE status IN ('pending','submitted')")
        return cur.fetchall()

    # ---- positions --------------------------------------------------------
    def upsert_position(self, position_id: str, **fields) -> None:
        existing = self._conn.execute(
            "SELECT 1 FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._conn.execute(
                f"UPDATE positions SET {sets} WHERE position_id=?",
                (*fields.values(), position_id),
            )
        else:
            cols = ", ".join(["position_id", *fields])
            marks = ",".join("?" * (len(fields) + 1))
            self._conn.execute(
                f"INSERT INTO positions ({cols}) VALUES ({marks})",
                (position_id, *fields.values()),
            )

    def record_peak_pnl(self, position_id: str, pnl: float) -> float:
        """Peak unrealised P&L ratchets up only — it is the reference the
        break-even stop is measured against, so it must not decay."""
        row = self._conn.execute(
            "SELECT peak_pnl FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()
        peak = float(row["peak_pnl"]) if row and row["peak_pnl"] is not None else 0.0
        if pnl > peak:
            self._conn.execute(
                "UPDATE positions SET peak_pnl=? WHERE position_id=?", (pnl, position_id)
            )
            return pnl
        return peak

    def close_position(
        self, position_id: str, barrier: str, label: int, realized_pnl: float, closed_at: str
    ) -> None:
        """Record the exit and its label. The barrier that closed the position
        is the training signal, so both are written together."""
        self._conn.execute(
            "UPDATE positions SET status='closed', exit_barrier=?, meta_label=?, "
            "realized_pnl=?, closed_at=?, exit_reason=? WHERE position_id=?",
            (barrier, label, realized_pnl, closed_at, barrier, position_id),
        )

    def training_rows(self) -> list[sqlite3.Row]:
        """Closed, labelled positions — the meta-labeler's dataset."""
        return self._conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND meta_label IS NOT NULL"
        ).fetchall()

    def open_positions(self) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM positions WHERE status IN ('opening','open','closing')"
        )
        return cur.fetchall()

    def total_heat(self) -> float:
        """Sum of max-loss across positions that are on or going on."""
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(max_loss), 0) AS heat FROM positions "
            "WHERE status IN ('opening','open','closing')"
        )
        return float(cur.fetchone()["heat"])

    # ---- system state -----------------------------------------------------
    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now()),
        )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
