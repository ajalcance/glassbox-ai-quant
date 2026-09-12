"""Repair tests.

This module writes to the position ledger, so its tests are mostly about what
it REFUSES to do. The prices below are the real ones from 11 Sep — the day the
supervisor flattened two live positions out of band and left the store halted
on `local-only` for the last three hours of the session.
"""

import json
from datetime import UTC, datetime

import pytest

from glassbox.data.activities import Fill
from glassbox.repair import REPAIR_BARRIER, apply, plan

CLOSED_AT = datetime(2026, 9, 11, 17, 5, 41, tzinfo=UTC)


def row(**over):
    base = {
        "position_id": "pos-QQQ-1",
        "underlying": "QQQ",
        "status": "open",
        "qty": 1,
        "entry_price": 2.11,
        "opened_at": "2026-09-11T13:45:48+00:00",
        "legs_json": json.dumps(
            [
                {
                    "symbol": "QQQ260914P00715000",
                    "side": "long",
                    "ratio_qty": 1,
                    "strike": 715.0,
                    "right": "put",
                    "expiry": "2026-09-14",
                },
                {
                    "symbol": "QQQ260914P00705000",
                    "side": "short",
                    "ratio_qty": 1,
                    "strike": 705.0,
                    "right": "put",
                    "expiry": "2026-09-14",
                },
            ]
        ),
    }
    base.update(over)
    return base


QQQ_FILLS = [
    Fill("QQQ260914P00715000", "sell", 1, 2.15, "2026-09-11T17:05:41+00:00"),
    Fill("QQQ260914P00705000", "buy", 1, 0.45, "2026-09-11T17:05:41+00:00"),
]


def test_rebuilds_the_exit_from_the_brokers_own_fills():
    """QQQ 715/705 put debit: entered at 2.11 debit, flattened for 1.70 credit.
    The broker's account shows -$91.30 across the day; this leg of it is -$41."""
    (r,) = plan([row()], {}, QQQ_FILLS)
    assert r.ok, r.reason
    assert r.close_price == pytest.approx(-1.70), "negative because the exit paid us"
    assert r.realized_pnl == pytest.approx(-41.0)


def test_credit_structure_signs_survive_the_round_trip():
    """ORCL 148/146 bull put: took $52 of credit, bought back for $77."""
    legs = json.dumps(
        [
            {
                "symbol": "ORCL260918P00148000",
                "side": "short",
                "ratio_qty": 1,
                "strike": 148.0,
                "right": "put",
                "expiry": "2026-09-18",
            },
            {
                "symbol": "ORCL260918P00146000",
                "side": "long",
                "ratio_qty": 1,
                "strike": 146.0,
                "right": "put",
                "expiry": "2026-09-18",
            },
        ]
    )
    fills = [
        Fill("ORCL260918P00148000", "buy", 1, 2.65, "2026-09-11T17:05:41+00:00"),
        Fill("ORCL260918P00146000", "sell", 1, 1.88, "2026-09-11T17:05:41+00:00"),
    ]
    (r,) = plan(
        [row(position_id="pos-ORCL-2", underlying="ORCL", entry_price=-0.52, legs_json=legs)],
        {},
        fills,
    )
    assert r.ok and r.close_price == pytest.approx(0.77)
    assert r.realized_pnl == pytest.approx(-25.0)


def test_a_position_the_broker_still_holds_is_never_touched():
    """The broker is truth. A leg still on the book means the flatten was
    partial, and a partial flatten is a situation for a person."""
    (r,) = plan([row()], {"QQQ260914P00715000": 1}, QQQ_FILLS)
    assert not r.ok and "still holds" in r.reason


def test_shared_legs_are_refused_rather_than_guessed():
    """Two positions sharing a contract cannot have their fills told apart by
    symbol. Guessing here would book a fabricated P&L into the ledger."""
    other = row(position_id="pos-QQQ-2", entry_price=1.90)
    results = plan([row(), other], {}, QQQ_FILLS)
    assert len(results) == 2
    assert all(not r.ok and "shared" in r.reason for r in results)


def test_missing_fills_are_refused():
    """No fill, no exit price. A mark or a mid would be an invention."""
    (r,) = plan([row()], {}, [QQQ_FILLS[0]])
    assert not r.ok and "no matching close fills" in r.reason


def test_partial_leg_quantity_is_refused():
    """A 2-lot whose close only shows 1 contract is not a closed position."""
    (r,) = plan([row(qty=2)], {}, QQQ_FILLS)
    assert not r.ok and "1/2 closed" in r.reason


def test_fills_before_the_position_opened_are_ignored():
    """An earlier trade in the same contract is not this position's exit."""
    stale = [
        Fill("QQQ260914P00715000", "sell", 1, 2.15, "2026-09-10T15:00:00+00:00"),
        Fill("QQQ260914P00705000", "buy", 1, 0.45, "2026-09-10T15:00:00+00:00"),
    ]
    (r,) = plan([row()], {}, stale)
    assert not r.ok and "no matching close fills" in r.reason


def test_wrong_direction_fills_do_not_count_as_a_close():
    """Buying more of a leg we are long is an add, not an exit."""
    adds = [
        Fill("QQQ260914P00715000", "buy", 1, 2.15, "2026-09-11T17:05:41+00:00"),
        Fill("QQQ260914P00705000", "sell", 1, 0.45, "2026-09-11T17:05:41+00:00"),
    ]
    (r,) = plan([row()], {}, adds)
    assert not r.ok


def test_closed_positions_are_not_candidates():
    assert plan([row(status="closed")], {}, QQQ_FILLS) == []
    assert plan([row(status="failed")], {}, QQQ_FILLS) == []


def test_apply_writes_the_exit_without_labelling_it(tmp_path):
    """An infrastructure failure is not a trading outcome. `training_rows()`
    selects on `meta_label IS NOT NULL`, so a NULL label keeps the supervisor's
    emergency flatten out of the meta-labeler's dataset instead of teaching it
    that this setup loses money."""
    from glassbox.audit import AuditLog
    from glassbox.store import Store

    store = Store(tmp_path / "s.db")
    store.upsert_position(
        "pos-QQQ-1",
        signal_id="QQQ-1",
        underlying="QQQ",
        kind="put_debit_spread",
        legs_json=row()["legs_json"],
        qty=1,
        entry_price=2.11,
        max_loss=211.0,
        status="open",
        opened_at=row()["opened_at"],
    )
    audit = AuditLog(tmp_path, role="trader")
    written = apply(store, audit, plan(store.open_positions(), {}, QQQ_FILLS), CLOSED_AT)

    assert written == 1
    assert store.open_positions() == []
    saved = store._conn.execute(
        "SELECT * FROM positions WHERE position_id='pos-QQQ-1'"
    ).fetchone()
    assert saved["status"] == "closed"
    assert saved["exit_barrier"] == REPAIR_BARRIER
    assert saved["realized_pnl"] == pytest.approx(-41.0)
    assert saved["meta_label"] is None, "must not enter the training set"
    assert store.training_rows() == []
    store.close()


def test_apply_leaves_unresolved_positions_open(tmp_path):
    """An unexplained divergence must keep the reconcile halt standing."""
    from glassbox.audit import AuditLog
    from glassbox.store import Store

    store = Store(tmp_path / "s.db")
    store.upsert_position(
        "pos-QQQ-1",
        signal_id="QQQ-1",
        underlying="QQQ",
        kind="put_debit_spread",
        legs_json=row()["legs_json"],
        qty=1,
        entry_price=2.11,
        max_loss=211.0,
        status="open",
        opened_at=row()["opened_at"],
    )
    audit = AuditLog(tmp_path, role="trader")
    resolutions = plan(store.open_positions(), {"QQQ260914P00715000": 1}, QQQ_FILLS)
    assert apply(store, audit, resolutions, CLOSED_AT) == 0
    assert len(store.open_positions()) == 1
    store.close()


def test_signed_cash_matches_the_price_convention():
    """Positive means we paid, the same orientation `lifecycle._on_fill` uses."""
    assert Fill("X", "buy", 1, 2.5, "t").signed_cash == 2.5
    assert Fill("X", "sell", 1, 2.5, "t").signed_cash == -2.5
    assert Fill("X", "sell_short", 1, 2.5, "t").signed_cash == -2.5


def test_repaired_exit_agrees_with_the_live_close_formula():
    """The repair must produce the same number the normal path would have, or
    the ledger says two different things depending on who closed the position."""
    entry, qty = 2.11, 1
    (r,) = plan([row()], {}, QQQ_FILLS)
    lifecycle_formula = (-r.close_price - entry) * 100 * qty
    assert r.realized_pnl == pytest.approx(lifecycle_formula)
