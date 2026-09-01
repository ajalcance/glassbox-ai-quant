"""SQLite state store. The broker is truth; this is a cache and an audit aid.

All SQL lives here — business logic never writes raw SQL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
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
    regime            TEXT,
    features_json     TEXT,
    thesis_direction  TEXT,
    thesis_move_pct   REAL,
    entry_spot        REAL,
    peak_pnl      REAL NOT NULL DEFAULT 0,
    exit_barrier  TEXT,
    meta_label    INTEGER,
    opened_at     TEXT,
    closed_at     TEXT,
    exit_reason   TEXT,
    realized_pnl  REAL
);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id      TEXT PRIMARY KEY,
    signal_id          TEXT,
    symbol             TEXT NOT NULL,
    predicted_at       TEXT NOT NULL,
    spot_at_prediction REAL NOT NULL,
    expected_move_pct  REAL NOT NULL,
    direction          TEXT NOT NULL,
    confidence         REAL,
    horizon_hours      REAL NOT NULL,
    implied_move_pct   REAL,
    resolve_after      TEXT NOT NULL,
    traded             INTEGER NOT NULL DEFAULT 0,
    resolved_at        TEXT,
    actual_move_pct    REAL,
    actual_signed_pct  REAL
);

CREATE INDEX IF NOT EXISTS idx_predictions_unresolved
    ON predictions(resolved_at, resolve_after);

CREATE TABLE IF NOT EXISTS bandit_state (
    arm        TEXT NOT NULL,
    regime     TEXT NOT NULL,
    alpha      REAL NOT NULL DEFAULT 1.0,
    beta       REAL NOT NULL DEFAULT 1.0,
    pulls      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (arm, regime)
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
    """One connection per thread. The trader's news-stream thread and its main
    loop share a single Store instance, and sqlite3 connections refuse use from
    a thread other than their creator — a shared connection would make every
    socket-delivered story fail at its first store access. WAL mode makes the
    per-thread connections safe against each other."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn.executescript(SCHEMA)
        self._migrate()

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently leaves an older table alone, so a
        store built by a previous version would be missing newer columns and
        fail at write time — mid-session, holding real positions.
        """
        wanted = {
            "positions": {
                "horizon_hours": "REAL",
                "regime": "TEXT",
                "features_json": "TEXT",
                "thesis_direction": "TEXT",
                "thesis_move_pct": "REAL",
                "entry_spot": "REAL",
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
        """Close the calling thread's connection. Other threads' connections
        (the trader's daemon stream thread) end with the process."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

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

    def orders_created_since(self, iso_ts: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE created_at >= ?", (iso_ts,)
        ).fetchone()
        return int(row["n"])

    def recent_loss_streak(self) -> int:
        """Consecutive losses among the most recently closed positions."""
        rows = self._conn.execute(
            "SELECT meta_label FROM positions WHERE status='closed' "
            "AND meta_label IS NOT NULL ORDER BY closed_at DESC LIMIT 10"
        ).fetchall()
        streak = 0
        for r in rows:
            if int(r["meta_label"]) == 0:
                streak += 1
            else:
                break
        return streak

    def orders_in_flight(self) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM orders WHERE status IN ('pending','submitted')")
        return cur.fetchall()

    def latest_order_for(self, position_id: str, intent: str) -> sqlite3.Row | None:
        """Most recent order of the given intent for a position, any status."""
        cur = self._conn.execute(
            "SELECT * FROM orders WHERE position_id=? AND intent=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (position_id, intent),
        )
        return cur.fetchone()

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

    # ---- predictions ------------------------------------------------------
    def record_prediction(self, prediction_id: str, **fields) -> None:
        """Every analyst estimate, whether or not it became a trade.

        Vetoed and untraded signals are the majority of the sample and are just
        as informative about whether the model over-estimates, so they are kept.
        """
        cols = ", ".join(["prediction_id", *fields])
        marks = ",".join("?" * (len(fields) + 1))
        self._conn.execute(
            f"INSERT OR IGNORE INTO predictions ({cols}) VALUES ({marks})",
            (prediction_id, *fields.values()),
        )

    def due_predictions(self, now_iso: str, limit: int = 200) -> list[sqlite3.Row]:
        """Predictions whose horizon has elapsed and which are not yet scored."""
        return self._conn.execute(
            "SELECT * FROM predictions WHERE resolved_at IS NULL AND resolve_after <= ? "
            "ORDER BY resolve_after LIMIT ?",
            (now_iso, limit),
        ).fetchall()

    def resolve_prediction(
        self, prediction_id: str, actual_move_pct: float, actual_signed_pct: float
    ) -> None:
        self._conn.execute(
            "UPDATE predictions SET resolved_at=?, actual_move_pct=?, actual_signed_pct=? "
            "WHERE prediction_id=?",
            (_now(), actual_move_pct, actual_signed_pct, prediction_id),
        )

    def resolved_predictions(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM predictions WHERE resolved_at IS NOT NULL"
        ).fetchall()

    def mark_prediction_traded(self, signal_id: str) -> None:
        self._conn.execute("UPDATE predictions SET traded=1 WHERE signal_id=?", (signal_id,))

    # ---- bandit -----------------------------------------------------------
    def bandit_posteriors(self, regime: str) -> dict[str, tuple[float, float, int]]:
        rows = self._conn.execute(
            "SELECT arm, alpha, beta, pulls FROM bandit_state WHERE regime=?", (regime,)
        ).fetchall()
        return {r["arm"]: (r["alpha"], r["beta"], r["pulls"]) for r in rows}

    def update_bandit(self, arm: str, regime: str, won: bool) -> None:
        """One Bernoulli observation. Alpha counts wins, beta counts losses."""
        self._conn.execute(
            "INSERT INTO bandit_state (arm, regime, alpha, beta, pulls, updated_at) "
            "VALUES (?,?,1.0,1.0,0,?) ON CONFLICT(arm, regime) DO NOTHING",
            (arm, regime, _now()),
        )
        column = "alpha" if won else "beta"
        self._conn.execute(
            f"UPDATE bandit_state SET {column}={column}+1, pulls=pulls+1, updated_at=? "
            "WHERE arm=? AND regime=?",
            (_now(), arm, regime),
        )

    def all_bandit_state(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM bandit_state ORDER BY regime, arm").fetchall()

    def positions_opened_on(self, day_prefix: str) -> int:
        """How many positions were opened on a given day (ISO date prefix)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE opened_at LIKE ?",
            (f"{day_prefix}%",),
        ).fetchone()
        return int(row["n"])

    def position_for_signal(self, signal_id: str) -> sqlite3.Row | None:
        """Any position ever opened from this signal, whatever its status.

        The durable half of news deduplication: the in-memory seen set dies
        with the process, but a signal that already became a position must not
        become a second one when the poller replays it after a restart.
        """
        return self._conn.execute(
            "SELECT * FROM positions WHERE signal_id=? LIMIT 1", (signal_id,)
        ).fetchone()

    def get_position(self, position_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()

    def open_positions_for(self, underlying: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM positions WHERE underlying=? AND status='open'", (underlying,)
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
