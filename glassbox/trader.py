"""The orchestrator — where a news item becomes a position, or doesn't.

Dependencies are injected rather than constructed, so the entire pipeline runs
against stubs in tests. That matters more than usual here: the alternative is
discovering a wiring bug at 3am against a live session.

The pipeline is deliberately a straight line with one exit at every stage. Most
news dies at the filter; most of what survives fails the edge test; most of what
passes the edge test is still refused by the gate. Every one of those exits is
written to the audit log with its reason, which is what makes the veto log — the
record of what we chose *not* to do — the most informative artifact we produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from glassbox.audit import AuditLog
from glassbox.chain import (
    NoSuitableStrikesError,
    atm_straddle_mid,
    build_structure,
    structure_liquidity,
)
from glassbox.execution.ids import client_order_id
from glassbox.gate import GateContext, evaluate
from glassbox.llm import LlmSchemaError, LlmUnavailableError
from glassbox.manage import Action, PositionView, evaluate_position
from glassbox.portfolio import snapshot
from glassbox.reconcile import is_halted
from glassbox.signal.analyst import analyse
from glassbox.signal.edge import evaluate_edge
from glassbox.signal.filter import NewsItem
from glassbox.sizing import size_position
from glassbox.structures import (
    ImplausiblePricingError,
    UndefinedRiskError,
    max_loss_per_spread,
    structure_key,
)


@dataclass(frozen=True, slots=True)
class MarketState:
    """Everything about the session and account the pipeline needs."""

    is_open: bool
    minutes_since_open: int
    minutes_to_close: int
    equity: float
    daily_pnl_pct: float
    drawdown_pct: float
    orders_last_minute: int = 0
    new_positions_today: int = 0
    loss_streak: int = 0


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened to one news item, and why."""

    stage: str
    traded: bool
    reason: str
    signal_id: str | None = None

    def __str__(self) -> str:
        return f"[{self.stage}] {'TRADED' if self.traded else 'no trade'}: {self.reason}"


class Trader:
    def __init__(
        self, *, cfg, store, audit: AuditLog, router, news_filter, llm, market_data, clock
    ):
        self.cfg = cfg
        self.store = store
        self.audit = audit
        self.router = router
        self.filter = news_filter
        self.llm = llm
        self.data = market_data  # supplies spot, chain, correlations
        self.clock = clock  # callable -> aware datetime

    # -- helpers ----------------------------------------------------------
    def _drop(self, stage: str, item: NewsItem, reason: str, **extra) -> Outcome:
        self.audit.append(
            "signal_dropped",
            {
                "stage": stage,
                "symbol": item.symbol,
                "news_id": item.id,
                "headline": item.headline[:200],
                "reason": reason,
                **extra,
            },
        )
        return Outcome(stage, False, reason)

    # -- the pipeline -----------------------------------------------------
    def process_news(self, item: NewsItem, market: MarketState) -> Outcome:
        """One news item, start to finish. Never raises for ordinary refusals."""
        signal_id = f"{item.symbol}-{item.id}"

        if is_halted(self.store):
            return self._drop("halt", item, "system halted")

        # 0. If we could not act on this even with a perfect signal, do not pay
        # a model to read it. The gate would veto a closed market anyway, and
        # news arrives around the clock — without this the system would spend
        # every night analysing stories it can never trade.
        if not market.is_open:
            return self._drop("market_closed", item, "market closed")

        # 1. deterministic filter — kills most of the stream at no model cost
        verdict = self.filter.evaluate(item, now=self.clock())
        if not verdict.passed:
            return self._drop("filter", item, verdict.reason)

        # 2. the analyst reads the text; a bad response is dropped, never repaired
        try:
            view = analyse(
                self.llm,
                self.cfg,
                symbol=item.symbol,
                headline=item.headline,
                summary=item.summary,
                source=item.source,
            )
        except (LlmUnavailableError, LlmSchemaError) as e:
            return self._drop("analyst", item, f"{type(e).__name__}: {e}")

        self.audit.append(
            "analyst_view",
            {
                "signal_id": signal_id,
                "symbol": item.symbol,
                "headline": item.headline[:200],
                "model": self.cfg.llm.analyst_model,
                **view.model_dump(),
            },
        )

        # 3. market context
        try:
            spot = self.data.spot(item.symbol)
            chain = self.data.chain(item.symbol, view.horizon_hours)
            straddle = atm_straddle_mid(chain, spot)
        except (NoSuitableStrikesError, ValueError, KeyError) as e:
            return self._drop("market_data", item, f"{type(e).__name__}: {e}", signal_id=signal_id)

        hours_to_expiry = self.data.hours_to_expiry(chain)

        # 4. the edge test — is the move bigger or smaller than what is priced?
        edge = evaluate_edge(
            expected_move_pct=view.expected_move_pct,
            direction=view.direction,
            confidence=view.confidence,
            straddle_mid=straddle,
            spot=spot,
            hours_to_expiry=hours_to_expiry,
            horizon_hours=view.horizon_hours,
            cfg=self.cfg,
        )
        self.audit.append(
            "edge_test",
            {
                "signal_id": signal_id,
                "verdict": str(edge.verdict),
                "ratio": edge.ratio,
                "expected_move_pct": edge.expected_move_pct,
                "implied_move_pct": edge.implied_move_pct,
                "detail": edge.detail,
            },
        )
        if not edge.tradable:
            return Outcome("edge", False, edge.detail, signal_id)

        # 5. express the view in a defined-risk structure
        kind = self.select_structure(edge.eligible_structures)
        try:
            structure, net_price = build_structure(
                kind, chain, spot, view.expected_move_pct, item.symbol
            )
            risk = max_loss_per_spread(structure, net_price)
        except (NoSuitableStrikesError, UndefinedRiskError, ImplausiblePricingError) as e:
            return self._drop("chain", item, f"{type(e).__name__}: {e}", signal_id=signal_id)

        # 6. sizing — models influence size, never limits
        sizing = size_position(
            equity=market.equity,
            max_loss_per_spread=risk,
            meta_label_p=self.meta_label(view, edge),
            cfg=self.cfg,
            underlying_vol=self.data.realized_vol(item.symbol),
            loss_streak=market.loss_streak,
        )
        if not sizing.approved:
            return self._drop("sizing", item, sizing.reason, signal_id=signal_id)

        # 7. the gate — deterministic, non-bypassable
        spread_pct, oi = structure_liquidity(structure, chain)
        ctx = GateContext(
            structure=structure,
            qty=sizing.qty,
            max_loss_per_spread=risk,
            meta_label_p=self.meta_label(view, edge),
            equity=market.equity,
            daily_pnl_pct=market.daily_pnl_pct,
            drawdown_pct=market.drawdown_pct,
            market_open=market.is_open,
            minutes_since_open=market.minutes_since_open,
            minutes_to_close=market.minutes_to_close,
            hours_to_expiry=hours_to_expiry,
            halted=is_halted(self.store),
            kill_switch=self.data.kill_switch(),
            portfolio=snapshot(self.store),
            post_trade_greeks=self.data.post_trade_greeks(structure, sizing.qty),
            correlations=self.data.correlations(),
            spread_pct_of_mid=spread_pct,
            open_interest=oi,
            orders_last_minute=market.orders_last_minute,
            new_positions_today=market.new_positions_today,
            duplicate_open=self.has_duplicate(structure),
        )
        decision = evaluate(ctx, self.cfg)
        self.audit.append(
            "gate",
            {
                "signal_id": signal_id,
                "structure": structure_key(structure),
                "qty": sizing.qty,
                "max_loss": risk * sizing.qty,
                "sizing_reason": sizing.reason,
                **decision.as_dict(),
            },
        )
        if not decision.approved:
            return Outcome("gate", False, decision.reason, signal_id)

        # 8. execute
        position_id = f"pos-{signal_id}"
        coid = client_order_id(signal_id, structure_key(structure))
        self.store.upsert_position(
            position_id,
            signal_id=signal_id,
            underlying=item.symbol,
            kind=str(kind),
            legs_json=self._legs_json(structure),
            qty=sizing.qty,
            entry_price=net_price,
            max_loss=risk * sizing.qty,
            status="opening",
            horizon_hours=view.horizon_hours,
            opened_at=self.clock().isoformat(),
        )
        self.router.submit_structure(structure, sizing.qty, net_price, coid, position_id)
        return Outcome(
            "executed",
            True,
            f"{kind} x{sizing.qty} at {net_price:+.2f} (risk ${risk * sizing.qty:,.0f})",
            signal_id,
        )

    # -- position management ---------------------------------------------
    def manage_positions(self, now: datetime, deadline: datetime | None = None) -> list[Outcome]:
        """Walk every open position through the triple barrier."""
        outcomes = []
        for row in self.store.open_positions():
            if row["status"] != "open":
                continue
            try:
                view = self._position_view(row)
            except (KeyError, ValueError) as e:
                self.audit.append(
                    "manage_error",
                    {"position_id": row["position_id"], "error": f"{type(e).__name__}: {e}"},
                )
                continue

            peak = self.store.record_peak_pnl(row["position_id"], view.unrealized_pnl)
            view = self._with_peak(view, peak)
            decision = evaluate_position(view, self.cfg, now, deadline)
            self.audit.append(
                "manage",
                {
                    "position_id": view.position_id,
                    "action": str(decision.action),
                    "barrier": str(decision.barrier),
                    "reason": decision.reason,
                    "unrealized_pnl": decision.unrealized_pnl,
                    "label": decision.label,
                },
            )
            if decision.action is Action.CLOSE:
                self._close(row, view, decision, now)
                outcomes.append(Outcome("manage", False, decision.reason, view.position_id))
        return outcomes

    def heartbeat(self) -> None:
        """The supervisor flattens the book if this stops advancing."""
        self.store.set_state("trader_heartbeat", self.clock().isoformat())

    # -- overridable hooks (the ML layer plugs in here) -------------------
    def select_structure(self, eligible):
        """Which structure to use. The bandit replaces this; until it has
        posteriors, the edge test's first choice is used."""
        return eligible[0]

    def meta_label(self, view, edge) -> float:
        """P(this signal is profitable). The meta-labeler replaces this; until
        it is trained, the analyst's own confidence is the honest stand-in."""
        return view.confidence

    # -- internals --------------------------------------------------------
    def has_duplicate(self, structure) -> bool:
        key = structure_key(structure)
        return any(
            structure_key(self._structure_from_row(r)) == key
            for r in self.store.open_positions()
            if r["underlying"] == structure.underlying
        )

    @staticmethod
    def _legs_json(structure) -> str:
        import json

        from glassbox.execution.router import legs_as_dicts

        return json.dumps(legs_as_dicts(structure))

    def _structure_from_row(self, row):
        import json
        from datetime import date as _date

        from glassbox.structures import Leg, LegSide, Right, Structure, StructureKind

        legs = tuple(
            Leg(
                symbol=leg_dict["symbol"],
                right=Right(leg_dict["right"]),
                strike=leg_dict["strike"],
                expiry=_date.fromisoformat(leg_dict["expiry"]),
                side=LegSide(leg_dict["side"]),
                ratio_qty=leg_dict["ratio_qty"],
            )
            for leg_dict in json.loads(row["legs_json"])
        )
        return Structure(StructureKind(row["kind"]), row["underlying"], legs)

    def _position_view(self, row) -> PositionView:
        structure = self._structure_from_row(row)
        return PositionView(
            position_id=row["position_id"],
            kind=structure.kind,
            qty=int(row["qty"]),
            entry_price=float(row["entry_price"]),
            current_price=self.data.structure_price(structure),
            max_loss_per_spread=float(row["max_loss"]) / max(int(row["qty"]), 1),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            horizon_hours=float(row["horizon_hours"] or 24.0),
            hours_to_expiry=self.data.structure_hours_to_expiry(structure),
            peak_pnl=float(row["peak_pnl"] or 0.0),
        )

    @staticmethod
    def _with_peak(view: PositionView, peak: float) -> PositionView:
        from dataclasses import replace

        return replace(view, peak_pnl=peak)

    def _close(self, row, view: PositionView, decision, now: datetime) -> None:
        from glassbox.execution.ids import close_order_id

        structure = self._structure_from_row(row)
        coid = close_order_id(view.position_id, str(decision.barrier))
        self.store.upsert_position(view.position_id, status="closing")
        self.router.submit_structure(
            structure, view.qty, view.current_price, coid, view.position_id, closing=True
        )
        self.store.close_position(
            view.position_id,
            str(decision.barrier),
            decision.label or 0,
            decision.unrealized_pnl,
            now.isoformat(),
        )
