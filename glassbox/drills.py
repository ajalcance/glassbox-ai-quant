"""Live drills against the paper account.

Everything here places **real orders that are intended to fill**. Unit tests and
dry runs prove the code is internally consistent; only a completed round trip
proves that fills, position state, barrier management, closing, P&L realisation,
labelling and the learning that depends on a closed position actually work.

    uv run python -m glassbox.drills list
    uv run python -m glassbox.drills round_trip
    uv run python -m glassbox.drills flatten
    uv run python -m glassbox.drills reconcile
    uv run python -m glassbox.drills cleanup

Objective is mechanical correctness, not P&L. A drill that loses a few dollars
and proves the close path works is a success. Size is deliberately one spread on
a liquid underlying so the worst case is small.

These run on the development account. Never point them at the contest account:
they would pollute the trading history the submission is judged on.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta

from glassbox.audit import AuditLog
from glassbox.chain import build_structure
from glassbox.clock import now_utc
from glassbox.config import load_config
from glassbox.data.alpaca_client import (
    option_data_client,
    stock_data_client,
    trading_client,
)
from glassbox.data.market import MarketData
from glassbox.execution.ids import client_order_id, close_order_id
from glassbox.execution.router import OrderRouter, legs_as_dicts
from glassbox.ml.bandit import ThompsonBandit, VolRegime
from glassbox.store import Store
from glassbox.structures import StructureKind, max_loss_per_spread, structure_key

# One spread, liquid underlying. The point is to exercise the code path, and the
# worst case should be small enough that a failed drill costs nothing that
# matters.
DRILL_SYMBOL = "SPY"
DRILL_QTY = 1
FILL_TIMEOUT = 90


class DrillFailed(Exception):
    """A drill assertion did not hold. Printed loudly; nothing is retried."""


class _LifecycleShim:
    """The minimum surface `lifecycle.sync` needs, so a drill can realise a
    close through the production path instead of computing its own P&L.

    Drills used to write the unrealised mark taken *before* the close and label
    from that, which meant they never executed `lifecycle._on_fill` — the exact
    function where the close-fill sign convention lives. The one test everyone
    trusted was bypassing the code under test.
    """

    def __init__(self, store, audit, router, cfg):
        self.store = store
        self.audit = audit
        self.router = router
        self.cfg = cfg

    def _structure_from_row(self, row):
        from glassbox.structures import Leg, LegSide, Right, Structure, StructureKind

        legs = tuple(
            Leg(
                l["symbol"],
                Right(l["right"]),
                float(l["strike"]),
                __import__("datetime").date.fromisoformat(l["expiry"]),
                LegSide(l["side"]),
                l.get("ratio_qty", 1),
            )
            for l in __import__("json").loads(row["legs_json"])
        )
        return Structure(StructureKind(row["kind"]), row["underlying"], legs)

    def feed_learners(self, row, label: int) -> None:
        """Same reward the live trader gives, from the same realised label."""
        ThompsonBandit(self.store).update(
            StructureKind(row["kind"]), VolRegime.NORMAL, won=bool(label)
        )


def _ctx():
    cfg = load_config()
    store = Store(cfg.paths.db)
    audit = AuditLog(cfg.paths.audit_dir, role="drills")
    client = trading_client()
    data = MarketData(
        trading_client=client,
        stock_client=stock_data_client(),
        option_client=option_data_client(),
        store=store,
        root=__import__("pathlib").Path("."),
    )
    return cfg, store, audit, client, data


def _require_market_open(client) -> None:
    clock = client.get_clock()
    if not clock.is_open:
        raise DrillFailed(
            f"market is closed — next open {clock.next_open:%Y-%m-%d %H:%M %Z}. "
            "These drills need live quotes and real fills."
        )


def _step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def _wait_for_fill(client, coid: str, timeout: int = FILL_TIMEOUT):
    """Poll until the order reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            order = client.get_order_by_client_id(coid)
        except Exception:  # noqa: BLE001 -- the order may not be visible yet
            time.sleep(2)
            continue
        status = str(order.status).split(".")[-1].lower()
        if status in ("filled", "canceled", "rejected", "expired"):
            return order, status
        time.sleep(2)
    return None, "timeout"


def _marketable_spread(data, kind=StructureKind.CALL_DEBIT_SPREAD):
    """Build a spread and price it to fill rather than to rest.

    A drill that rests unfilled proves nothing, so the limit crosses to the
    natural. That costs the spread width in slippage, which is the price of
    exercising the fill path.
    """
    spot = data.spot(DRILL_SYMBOL)
    chain = data.chain(DRILL_SYMBOL, horizon_hours=48)
    if not chain:
        raise DrillFailed(f"no tradable {DRILL_SYMBOL} chain")
    structure, mid_price = build_structure(kind, chain, spot, 1.5, DRILL_SYMBOL)
    # Pay up so it fills: a debit crosses upward, a credit accepts less.
    price = round(mid_price * 1.35 if mid_price > 0 else mid_price * 0.65, 2)
    return structure, mid_price, price


# --- drills ----------------------------------------------------------------


def drill_round_trip() -> int:
    """Open a real spread, verify every downstream effect, then close it."""
    cfg, store, audit, client, data = _ctx()
    try:
        _require_market_open(client)
        print(f"ROUND TRIP DRILL — account {client.get_account().account_number}")

        _step(1, "Building a marketable spread")
        structure, mid, price = _marketable_spread(data)
        risk = max_loss_per_spread(structure, mid)
        print(f"    {structure_key(structure)}")
        print(f"    mid {mid:+.2f} -> limit {price:+.2f}, max loss ${risk:.0f}/spread")

        _step(2, "Submitting through the real router")
        signal_id = f"drill-{now_utc():%H%M%S}"
        position_id = f"pos-{signal_id}"
        coid = client_order_id(signal_id, structure_key(structure))
        store.upsert_position(
            position_id,
            signal_id=signal_id,
            underlying=DRILL_SYMBOL,
            kind=str(structure.kind),
            legs_json=__import__("json").dumps(legs_as_dicts(structure)),
            qty=DRILL_QTY,
            entry_price=price,
            max_loss=risk * DRILL_QTY,
            status="opening",
            horizon_hours=24.0,
            opened_at=now_utc().isoformat(),
            regime=str(VolRegime.NORMAL),
        )
        router = OrderRouter(client, store, audit)
        router.submit_structure(structure, DRILL_QTY, price, coid, position_id)

        _step(3, f"Waiting up to {FILL_TIMEOUT}s for a fill")
        order, status = _wait_for_fill(client, coid)
        if status != "filled":
            raise DrillFailed(f"open order did not fill: {status}")
        print(f"    FILLED at {order.filled_avg_price}")
        # Record what we actually paid, not what we asked to pay. Storing the
        # limit here made every drill's P&L fiction: on 31 Aug the limit was
        # 4.51 and the fill 3.29, and the recorded entry was the 4.51.
        store.update_order(coid, status="filled", filled_price=float(order.filled_avg_price))
        store.upsert_position(
            position_id, status="open", entry_price=float(order.filled_avg_price)
        )

        _step(4, "Verifying the broker agrees we hold it")
        broker = client.get_all_positions()
        held = {p.symbol for p in broker}
        legs = {leg.symbol for leg in structure.legs}
        if not legs <= held:
            raise DrillFailed(f"broker missing legs: {legs - held}")
        print(f"    broker reports {len(legs & held)}/{len(legs)} legs")

        _step(5, "Reconciliation should be clean")
        from glassbox.reconcile import enforce

        result = enforce(store, audit, broker)
        if not result.ok:
            raise DrillFailed(f"reconciliation diverged: {result.reason}")
        print(f"    {result.reason}")

        _step(6, "Pricing the position and evaluating the barriers")
        current = data.structure_price(structure)
        from glassbox.manage import PositionView, evaluate_position

        view = PositionView(
            position_id=position_id,
            kind=structure.kind,
            qty=DRILL_QTY,
            entry_price=float(order.filled_avg_price),
            current_price=current,
            max_loss_per_spread=risk,
            opened_at=now_utc(),
            horizon_hours=24.0,
            hours_to_expiry=data.structure_hours_to_expiry(structure),
        )
        decision = evaluate_position(view, cfg, now_utc())
        print(
            f"    unrealised ${view.unrealized_pnl:+,.2f} -> {decision.barrier}: {decision.reason}"
        )

        _step(7, "Closing via a deadline that has already passed")
        past = now_utc() - timedelta(minutes=1)
        forced = evaluate_position(view, cfg, now_utc(), deadline=past)
        if not forced.should_close:
            raise DrillFailed("deadline did not force a close")
        close_coid = close_order_id(position_id, str(forced.barrier))
        router.submit_structure(
            structure,
            DRILL_QTY,
            round(current * 0.65, 2),
            close_coid,
            position_id,
            closing=True,
        )
        order, status = _wait_for_fill(client, close_coid)
        if status != "filled":
            raise DrillFailed(f"close order did not fill: {status}")
        print(f"    CLOSED at {order.filled_avg_price}")

        _step(8, "Realising through the production lifecycle")
        # Realise the close the way the live trader does, rather than writing a
        # P&L the drill computed itself. The old path stored the *unrealised*
        # mark from step 6 and labelled from that, so on 31 Aug it recorded a
        # +$5 win on a round trip that actually lost $8 — and fed that wrong
        # label to the bandit. It also meant this drill never executed
        # lifecycle._on_fill, which is exactly where the close-sign bug lived:
        # the one test everyone trusted was bypassing the code under test.
        from glassbox.execution import lifecycle

        # The router already recorded the close order as 'submitted', so it is
        # in orders_in_flight and sync() will poll it, see the fill, and
        # realise it through the same code the live trader uses.
        store.set_state(f"close_barrier:{position_id}", str(forced.barrier))
        for event in lifecycle.sync(_LifecycleShim(store, audit, router, cfg), now_utc()):
            print(f"    {event}")

        row = store.get_position(position_id)
        if row["status"] != "closed":
            raise DrillFailed(f"lifecycle did not close the position: {row['status']}")
        realized = float(row["realized_pnl"] or 0.0)
        entry = float(row["entry_price"] or 0.0)
        fill = float(order.filled_avg_price)
        print(f"    entry {entry:+.2f}  close fill {fill:+.2f}  realised ${realized:+,.2f}")
        # A defined-risk spread cannot realise much beyond its own max loss.
        # This is the assertion that would have caught the sign inversion.
        if abs(realized) > risk * DRILL_QTY * 1.5:
            raise DrillFailed(
                f"implausible realised P&L ${realized:,.2f} against max loss "
                f"${risk * DRILL_QTY:,.2f} — check the close-fill sign convention"
            )
        # The bandit was already fed by the lifecycle's feed_learners, from the
        # label it derived from the realised fill — the same path live trading
        # takes. Feeding it again here would double-count the pull.

        rows = store.training_rows()
        if not any(r["position_id"] == position_id for r in rows):
            raise DrillFailed("no training row was written for the closed position")
        posteriors = store.bandit_posteriors(str(VolRegime.NORMAL))
        if str(structure.kind) not in posteriors:
            raise DrillFailed("bandit posterior did not update")
        print(f"    training rows: {len(rows)} | bandit: {posteriors}")

        _step(9, "Broker should be flat again")
        remaining = {p.symbol for p in client.get_all_positions()} & legs
        if remaining:
            raise DrillFailed(f"legs still open at the broker: {remaining}")
        print("    flat")

        print("\nROUND TRIP DRILL PASSED")
        return 0
    finally:
        store.close()


def drill_flatten() -> int:
    """Open a position, engage the kill switch, verify the supervisor closes it."""
    from pathlib import Path

    from glassbox.supervisor.guards import KILL_SWITCH_FILE

    cfg, store, audit, client, data = _ctx()
    kill = Path(KILL_SWITCH_FILE)
    try:
        _require_market_open(client)
        print("FLATTEN DRILL")

        _step(1, "Opening a position to flatten")
        structure, _mid, price = _marketable_spread(data)
        coid = client_order_id(f"flatten-{now_utc():%H%M%S}", structure_key(structure))
        OrderRouter(client, store, audit).submit_structure(
            structure, DRILL_QTY, price, coid, "pos-flatten-drill"
        )
        order, status = _wait_for_fill(client, coid)
        if status != "filled":
            raise DrillFailed(f"could not open a position to flatten: {status}")
        print(f"    filled at {order.filled_avg_price}")

        _step(2, "Engaging the kill switch")
        kill.touch()

        _step(3, "Running one supervisor tick")
        from glassbox.supervisor.run import tick

        action = tick(store, audit, client, cfg, Path("."), dry_run=False)
        print(f"    supervisor: {action}")

        _step(4, "Broker should be flat")
        time.sleep(5)
        remaining = {p.symbol for p in client.get_all_positions()}
        if remaining & {leg.symbol for leg in structure.legs}:
            raise DrillFailed(f"supervisor did not flatten: {remaining}")
        print("    flat")

        print("\nFLATTEN DRILL PASSED")
        return 0
    finally:
        kill.unlink(missing_ok=True)
        from glassbox.reconcile import HALT_KEY

        store.set_state(HALT_KEY, "")
        store.close()


def drill_reconcile() -> int:
    """Induce a divergence and verify it halts trading. No orders placed."""
    _cfg, store, audit, client, _data = _ctx()
    try:
        print("RECONCILIATION DRILL (no orders placed)")
        from glassbox.reconcile import HALT_KEY, enforce, is_halted

        _step(1, "Recording a position the broker does not have")
        import json

        store.upsert_position(
            "pos-phantom",
            underlying="SPY",
            kind="bull_put_spread",
            legs_json=json.dumps(
                [
                    {"symbol": "SPY999999P00001000", "side": "short", "ratio_qty": 1},
                    {"symbol": "SPY999999P00000500", "side": "long", "ratio_qty": 1},
                ]
            ),
            qty=1,
            max_loss=100.0,
            status="open",
        )

        _step(2, "Reconciling against the real broker")
        result = enforce(store, audit, client.get_all_positions())
        if result.ok:
            raise DrillFailed("divergence was not detected")
        print(f"    detected: {result.reason[:120]}")

        _step(3, "System should be halted")
        if not is_halted(store):
            raise DrillFailed("divergence did not halt trading")
        print(f"    halted: {store.get_state(HALT_KEY)[:100]}")

        _step(4, "Clearing the phantom and confirming recovery")
        store.upsert_position("pos-phantom", status="closed")
        result = enforce(store, audit, client.get_all_positions())
        if not result.ok or is_halted(store):
            raise DrillFailed("halt did not clear once the divergence was resolved")
        print("    recovered")

        print("\nRECONCILIATION DRILL PASSED")
        return 0
    finally:
        store.close()


def drill_cleanup() -> int:
    """Close everything and clear drill state. Safe to run any time."""
    _cfg, store, _audit, client, _data = _ctx()
    try:
        print("CLEANUP")

        # Local drill state first. A failed drill leaves an open position in the
        # store; reconciliation then correctly flags a divergence against the
        # broker and every later drill fails on state the last one abandoned.
        stale = [
            r["position_id"]
            for r in store.open_positions()
            if str(r["position_id"]).startswith(("pos-drill-", "pos-sim-", "pos-flatten-"))
            or r["position_id"] == "pos-phantom"
        ]
        for position_id in stale:
            store.upsert_position(position_id, status="closed")
        print(f"  closed {len(stale)} stale drill position(s) in local state")

        orders = client.cancel_orders()
        print(f"  cancelled {len(orders or [])} open order(s)")
        try:
            closed = client.close_all_positions(cancel_orders=True)
            print(f"  closed {len(closed or [])} position(s)")
        except Exception as e:  # noqa: BLE001 -- nothing to close is not an error
            print(f"  close_all_positions: {type(e).__name__}: {e}")
        from glassbox.reconcile import HALT_KEY

        store.set_state(HALT_KEY, "")
        from pathlib import Path

        from glassbox.supervisor.guards import KILL_SWITCH_FILE

        Path(KILL_SWITCH_FILE).unlink(missing_ok=True)
        Path("KILL").unlink(missing_ok=True)  # legacy root-level sentinel
        print("  halt cleared, kill switch cleared")
        return 0
    finally:
        store.close()


class SimulatedBroker:
    """Fills instantly and remembers what it filled.

    Everything else in the simulate drill is real: the chain, the quotes, the
    structure, the max-loss arithmetic, the barrier evaluation, the store, the
    reconciliation logic and both learners. Only the fill is invented, because
    that is the one thing the market has to be open for.

    Fills are labelled `sim-` so a simulated position can never be mistaken for
    a real one in the audit log.
    """

    def __init__(self):
        self.legs: dict[str, int] = {}
        self.orders: list = []
        self.by_coid: dict[str, object] = {}

    def get_order_by_client_id(self, client_order_id: str):
        """Required by OrderRouter.poll, and therefore by lifecycle.sync.

        Without it the drill could not realise a close through the production
        path: poll would raise, the lifecycle would log a poll error and move
        on, and the position would sit open while the drill reported success.
        """
        order = self.by_coid.get(client_order_id)
        if order is not None:
            return order
        # An order this broker never issued is dead, not an error. Raising
        # here tripped the circuit breaker on the store's leftover in-flight
        # rows from earlier drills, and once open it blocked polling of the
        # drill's *own* close — the position stayed open and the drill failed
        # for a reason that had nothing to do with what it was testing.
        return type(
            "SimUnknownOrder",
            (),
            {"id": "sim-unknown", "status": "canceled", "filled_avg_price": None},
        )()

    def submit_order(self, request):
        self.orders.append(request)
        for leg in request.legs:
            signed = leg.ratio_qty * int(request.qty)
            if str(leg.side).split(".")[-1].lower() == "sell":
                signed = -signed
            self.legs[leg.symbol] = self.legs.get(leg.symbol, 0) + signed
        self.legs = {k: v for k, v in self.legs.items() if v != 0}
        order = type(
            "SimOrder",
            (),
            {
                "id": f"sim-{len(self.orders)}",
                "status": "filled",
                "filled_avg_price": request.limit_price,
            },
        )()
        self.by_coid[request.client_order_id] = order
        return order

    def get_all_positions(self):
        return [
            type("SimPos", (), {"symbol": sym, "qty": str(qty)})() for sym, qty in self.legs.items()
        ]

    def cancel_order_by_id(self, order_id):
        return None


def drill_simulate() -> int:
    """Full lifecycle with a simulated fill. Runs with the market closed."""
    import json

    cfg, store, audit, _client, data = _ctx()
    broker = SimulatedBroker()
    try:
        print("SIMULATED LIFECYCLE DRILL")
        print("  Real chain, real quotes, real barrier maths, real learners.")
        print("  Only the fill is simulated — that is the part needing an open market.\n")

        _step(1, "Building a spread from the live chain")
        structure, mid, _price = _marketable_spread(data)
        risk = max_loss_per_spread(structure, mid)
        print(f"    {structure_key(structure)}")
        print(f"    net {mid:+.2f}, max loss ${risk:.0f}/spread")

        _step(2, "Opening through the real router against a simulated broker")
        signal_id = f"sim-{now_utc():%H%M%S}"
        position_id = f"pos-{signal_id}"
        store.upsert_position(
            position_id,
            signal_id=signal_id,
            underlying=DRILL_SYMBOL,
            kind=str(structure.kind),
            legs_json=json.dumps(legs_as_dicts(structure)),
            qty=DRILL_QTY,
            entry_price=mid,
            max_loss=risk * DRILL_QTY,
            status="opening",
            horizon_hours=24.0,
            opened_at=now_utc().isoformat(),
            regime=str(VolRegime.NORMAL),
        )
        router = OrderRouter(broker, store, audit)
        coid = client_order_id(signal_id, structure_key(structure))
        order = router.submit_structure(structure, DRILL_QTY, mid, coid, position_id)
        store.upsert_position(position_id, status="open")
        print(f"    filled at {order.filled_avg_price} ({order.id})")

        _step(3, "Reconciling local state against the broker")
        from glassbox.reconcile import enforce

        result = enforce(store, audit, broker.get_all_positions())
        if not result.ok:
            raise DrillFailed(f"reconciliation diverged after opening: {result.reason}")
        print(f"    {result.reason}")

        _step(4, "Portfolio heat should reflect the open position")
        heat = store.total_heat()
        if heat <= 0:
            raise DrillFailed("open position contributed no heat")
        cap = cfg.account.starting_equity * cfg.risk.portfolio_heat_pct / 100
        print(f"    ${heat:,.0f} of ${cap:,.0f} cap")

        _step(5, "Pricing the position and evaluating the barriers")
        from glassbox.manage import PositionView, evaluate_position

        current = data.structure_price(structure)
        view = PositionView(
            position_id=position_id,
            kind=structure.kind,
            qty=DRILL_QTY,
            entry_price=mid,
            current_price=current,
            max_loss_per_spread=risk,
            opened_at=now_utc(),
            horizon_hours=24.0,
            hours_to_expiry=data.structure_hours_to_expiry(structure),
        )
        peak = store.record_peak_pnl(position_id, view.unrealized_pnl)
        decision = evaluate_position(view, cfg, now_utc())
        print(f"    unrealised ${view.unrealized_pnl:+,.2f} (peak ${peak:+,.2f})")
        print(f"    -> {decision.barrier}: {decision.reason}")

        _step(6, "Forcing a close through an elapsed deadline")
        past = now_utc() - timedelta(minutes=1)
        forced = evaluate_position(view, cfg, now_utc(), deadline=past)
        if not forced.should_close or forced.label is None:
            raise DrillFailed("deadline did not force a labelled close")
        close_coid = close_order_id(position_id, str(forced.barrier))
        # Same convention the live trader uses: the close limit is the negation
        # of the position's current value, because Alpaca's MLEG limit is
        # order-oriented while our prices are entry-oriented.
        router.submit_structure(
            structure, DRILL_QTY, round(-current, 2), close_coid, position_id, closing=True
        )
        # Realise through the production lifecycle rather than writing the
        # unrealised mark taken moments ago. Same flaw as round_trip had: the
        # drill recorded a P&L it computed itself and never executed
        # lifecycle._on_fill, so neither drill exercised the realisation code.
        from glassbox.execution import lifecycle

        store.set_state(f"close_barrier:{position_id}", str(forced.barrier))
        for event in lifecycle.sync(_LifecycleShim(store, audit, router, cfg), now_utc()):
            print(f"    {event}")
        row = store.get_position(position_id)
        if row["status"] != "closed":
            raise DrillFailed(f"lifecycle did not close the position: {row['status']}")
        realized = float(row["realized_pnl"] or 0.0)
        print(f"    closed on {forced.barrier}, label={row['meta_label']}, P&L ${realized:+,.2f}")
        if abs(realized) > risk * DRILL_QTY * 1.5:
            raise DrillFailed(
                f"implausible realised P&L ${realized:,.2f} against max loss "
                f"${risk * DRILL_QTY:,.2f} — check the close-fill sign convention"
            )

        _step(7, "Broker should be flat and heat released")
        if broker.legs:
            raise DrillFailed(f"legs still open after close: {broker.legs}")
        if store.total_heat() != 0:
            raise DrillFailed("closed position still contributing heat")
        if not enforce(store, audit, broker.get_all_positions()).ok:
            raise DrillFailed("reconciliation diverged after closing")
        print("    flat, heat released, reconciliation clean")

        _step(8, "Feeding the learners")
        # The lifecycle already fed the bandit through feed_learners, from the
        # label it derived from the realised close. Updating again here would
        # double-count the pull.
        posteriors = store.bandit_posteriors(str(VolRegime.NORMAL))
        if str(structure.kind) not in posteriors:
            raise DrillFailed("bandit posterior did not update")
        rows = [r for r in store.training_rows() if r["position_id"] == position_id]
        if not rows:
            raise DrillFailed("no training row written")
        print(f"    bandit {structure.kind!s}: {posteriors[str(structure.kind)]}")
        print(f"    training rows now: {len(store.training_rows())}")

        _step(9, "Audit chains should still verify")
        from glassbox.audit import verify_day

        ok, n, broken = verify_day(audit.dir)
        if not ok:
            raise DrillFailed(f"audit chain broken: {', '.join(broken)}")
        print(f"    verified, {n} records today across all roles")

        print("\nSIMULATED LIFECYCLE DRILL PASSED")
        print("Everything downstream of a fill is proven. Tonight's round_trip")
        print("drill proves the fill itself.")
        return 0
    finally:
        store.close()


DRILLS = {
    "simulate": drill_simulate,
    "round_trip": drill_round_trip,
    "flatten": drill_flatten,
    "reconcile": drill_reconcile,
    "cleanup": drill_cleanup,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Live drills on the paper account")
    parser.add_argument("drill", choices=[*DRILLS, "list"])
    args = parser.parse_args()

    if args.drill == "list":
        for name, fn in DRILLS.items():
            print(f"  {name:<12} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0

    try:
        return DRILLS[args.drill]()
    except DrillFailed as e:
        print(f"\nDRILL FAILED: {e}", file=sys.stderr)
        print("Run `python -m glassbox.drills cleanup` before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
