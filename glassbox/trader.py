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

import json
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
from glassbox.manage import (
    Action,
    Barrier,
    ManageDecision,
    PositionView,
    evaluate_position,
)
from glassbox.ml.bandit import classify_regime
from glassbox.ml.features import build_features
from glassbox.portfolio import snapshot
from glassbox.reconcile import is_halted
from glassbox.signal.analyst import analyse
from glassbox.signal.edge import evaluate_edge
from glassbox.signal.filter import NewsItem
from glassbox.sizing import drawdown_taper, heat_taper, size_position
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
        self,
        *,
        cfg,
        store,
        audit: AuditLog,
        router,
        news_filter,
        llm,
        market_data,
        clock,
        meta_labeler=None,
        bandit=None,
    ):
        self.cfg = cfg
        self.store = store
        self.audit = audit
        self.router = router
        self.filter = news_filter
        self.llm = llm
        self.data = market_data  # supplies spot, chain, correlations
        self.clock = clock  # callable -> aware datetime
        # Both optional. Absent, the hooks fall back to honest stand-ins rather
        # than inventing numbers — see meta_label() and select_structure().
        self.meta_labeler = meta_labeler
        self.bandit = bandit
        # Signals refused only on the opening-auction window, awaiting one
        # retry once it opens: signal_id -> (item, analyst view).
        self._deferred: dict[str, tuple] = {}
        self._spread_history: dict[str, list[tuple[datetime, float]]] = {}

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
    def process_news(self, item: NewsItem, market: MarketState, view_override=None) -> Outcome:
        """One news item, start to finish. Never raises for ordinary refusals.

        `view_override` carries a previously produced analyst view. Used only by
        the floor, which re-enters a signal that already passed the filter this
        morning: by mid-afternoon the same story would fail staleness and
        novelty against itself, and a second analyst call could quietly produce
        a different opinion than the one that made it the day's best idea.
        """
        signal_id = f"{item.symbol}-{item.id}"

        # A signal that already produced a position must never produce another.
        # The in-memory seen-news set dies with the process, the poller replays
        # the last 15 minutes of news on startup, and the bandit's structure
        # choice is stochastic — so a reprocessed signal can pick a different
        # structure, mint a different client_order_id, and walk past both the
        # broker's duplicate check and the gate's identical-structure check.
        # The positions table is the durable memory of what was acted on.
        if self.store.position_for_signal(signal_id) is not None:
            return self._drop("duplicate_signal", item, "position already exists for this signal",
                              signal_id=signal_id)

        if is_halted(self.store):
            return self._drop("halt", item, "system halted")

        # 0. If we could not act on this even with a perfect signal, do not pay
        # a model to read it. The gate would veto a closed market anyway, and
        # news arrives around the clock — without this the system would spend
        # every night analysing stories it can never trade.
        if not market.is_open:
            return self._drop("market_closed", item, "market closed")

        # 1. deterministic filter — kills most of the stream at no model cost.
        # A floor re-entry skips it: the story passed this morning, and would
        # now fail staleness and novelty against itself.
        if view_override is None:
            verdict = self.filter.evaluate(item, now=self.clock())
            if not verdict.passed:
                return self._drop("filter", item, verdict.reason)
            novelty = verdict.novelty
        else:
            novelty = 1.0

        # 2. the analyst reads the text; a bad response is dropped, never repaired
        if view_override is not None:
            view = view_override
        else:
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

        # The analyst has spoken. Before asking whether to open anything, ask
        # whether this reverses a view we are already holding — one model call,
        # two consumers.
        if not getattr(self, "_floor_mode", False):
            self.check_contradiction(item.symbol, view)

        # 3. market context
        try:
            spot = self.data.spot(item.symbol)
            chain = self.data.chain(item.symbol, view.horizon_hours)
            straddle = atm_straddle_mid(chain, spot)
        except (NoSuitableStrikesError, ValueError, KeyError) as e:
            return self._drop("market_data", item, f"{type(e).__name__}: {e}", signal_id=signal_id)

        hours_to_expiry = self.data.hours_to_expiry(chain)

        # How much of the expected move has the market already made? News up to
        # two hours old is accepted, and the reaction may be over by the time we
        # see it.
        realized = None
        if self.cfg.signal.consume_realized_move and hasattr(self.data, "move_since"):
            age_minutes = (self.clock() - item.created_at).total_seconds() / 60
            if age_minutes >= self.cfg.signal.min_minutes_for_reaction:
                try:
                    realized = self.data.move_since(item.symbol, item.created_at)
                except Exception:  # noqa: BLE001 -- unmeasurable discounts nothing
                    realized = None

        # Environment: regime scales conviction, macro proximity constrains.
        regime_reading = self.regime_reading()
        macro_window = self.macro_window()
        vrp_shift = regime_reading.vrp_shift(self.cfg) if regime_reading else 0.0

        forecast = None
        if hasattr(self.data, "forecast_move_pct"):
            try:
                forecast = self.data.forecast_move_pct(item.symbol, hours_to_expiry)
            except Exception:  # noqa: BLE001 -- the vol model is a second opinion
                forecast = None

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
            realized_move_pct=realized,
            forecast_move_pct=forecast,
            vrp_shift=vrp_shift,
            relax_band=getattr(self, "_floor_mode", False),
        )
        self.audit.append(
            "edge_test",
            {
                "signal_id": signal_id,
                "verdict": str(edge.verdict),
                "ratio": edge.ratio,
                "expected_move_pct": edge.expected_move_pct,
                "raw_expected_move_pct": edge.raw_expected_move_pct,
                "realized_move_pct": edge.realized_move_pct,
                "implied_move_pct": edge.implied_move_pct,
                "forecast_move_pct": forecast,
                "vrp_ratio": edge.vrp_ratio,
                "vrp_shift": vrp_shift,
                "regime": regime_reading.as_dict() if regime_reading else None,
                "macro": macro_window.as_dict() if macro_window else None,
                "detail": edge.detail,
            },
        )
        # Recorded whether or not this becomes a trade. Untraded signals are
        # most of the sample and say just as much about whether the analyst
        # over-estimates, which is what every ratio threshold rests on.
        #
        # A floor re-entry is deliberately excluded: it replays a view already
        # recorded this morning, so recording it again would count one estimate
        # twice and bias the calibration toward whatever the floor happens to
        # pick — the opposite of a representative sample.
        if not getattr(self, "_floor_mode", False):
            try:
                from glassbox import predictions

                predictions.record(
                    self.store,
                    signal_id=signal_id,
                    symbol=item.symbol,
                    spot=spot,
                    view=view,
                    implied_move_pct=edge.implied_move_pct,
                    now=self.clock(),
                )
            except Exception as e:  # noqa: BLE001 -- instrumentation must never
                # be able to prevent or alter a trading decision.
                self.audit.append(
                    "prediction_record_error", {"signal_id": signal_id, "error": str(e)}
                )

        if not edge.tradable:
            # A signal refused ONLY for sitting inside the ratio band is a
            # candidate for the daily best-idea floor. Confidence, bad-data and
            # VRP refusals are real filters and disqualify outright.
            if "fairly priced" in edge.detail:
                self._record_floor_candidate(item, view, edge)
            return Outcome("edge", False, edge.detail, signal_id)

        # An intraday thesis is closed at the bell rather than carried
        # overnight. The time barrier would otherwise fire hours after the
        # market shut, turning a four-hour view into an eighteen-hour hold on a
        # thesis that expired at the close.
        effective_horizon = self.effective_horizon(view.horizon_hours, market)

        # 5. express the view in a defined-risk structure
        realized_vol = self.data.realized_vol(item.symbol)
        regime = classify_regime(realized_vol, self.cfg.ml.vol_regime_bounds)
        kind, arm_detail = self.select_structure(edge.eligible_structures, regime)
        try:
            structure, net_price = build_structure(
                kind, chain, spot, view.expected_move_pct, item.symbol
            )
            risk = max_loss_per_spread(structure, net_price)
        except (NoSuitableStrikesError, UndefinedRiskError, ImplausiblePricingError) as e:
            return self._drop("chain", item, f"{type(e).__name__}: {e}", signal_id=signal_id)

        # 6. sizing — models influence size, never limits
        spread_pct, oi = structure_liquidity(structure, chain)
        spread_pct = self._windowed_spread(structure, chain, spread_pct)
        features = build_features(
            view=view,
            edge=edge,
            hours_to_expiry=hours_to_expiry,
            realized_vol=realized_vol,
            spread_pct_of_mid=spread_pct,
            is_credit=structure.is_credit,
            novelty=novelty,
        )
        p, label_detail = self.meta_label(features, view.confidence)
        context_mult = 1.0
        context_parts = []
        if regime_reading is not None and regime_reading.known:
            m = regime_reading.size_multiplier(self.cfg)
            context_mult *= m
            context_parts.append(f"regime {m:.2f}x")
        if macro_window is not None and macro_window.active:
            context_mult *= self.cfg.macro.near_event_size_factor
            context_parts.append(f"macro {self.cfg.macro.near_event_size_factor:.2f}x")
        # Budget tapers: approaching a limit should mean smaller bites, not
        # identical ones until the gate slams shut.
        portfolio = snapshot(self.store)
        heat_mult = heat_taper(portfolio.heat, market.equity, self.cfg)
        if heat_mult < 1.0:
            context_mult *= heat_mult
            context_parts.append(f"heat {heat_mult:.2f}x")
        dd_mult = drawdown_taper(market.daily_pnl_pct, self.cfg)
        if dd_mult < 1.0:
            context_mult *= dd_mult
            context_parts.append(f"drawdown {dd_mult:.2f}x")
        if getattr(self, "_floor_mode", False):
            # A floor trade is a best idea below the organic bar; it never
            # carries organic conviction, so it never carries organic size.
            context_mult *= self.cfg.floor.size_factor
            context_parts.append(f"floor {self.cfg.floor.size_factor:.2f}x")
        sizing = size_position(
            equity=market.equity,
            max_loss_per_spread=risk,
            meta_label_p=p,
            cfg=self.cfg,
            underlying_vol=realized_vol,
            target_vol=self.cfg.sizing.target_daily_vol,
            loss_streak=market.loss_streak,
            context_multiplier=context_mult,
        )
        self.audit.append(
            "ml",
            {
                "signal_id": signal_id,
                "regime": str(regime),
                "arm": str(kind),
                "arm_detail": arm_detail,
                "meta_label_p": p,
                "meta_label_detail": label_detail,
                "context_multiplier": context_mult,
                "context_detail": ", ".join(context_parts) or "none",
                "features": features.as_dict(),
            },
        )
        if not sizing.approved:
            return self._drop("sizing", item, sizing.reason, signal_id=signal_id)

        # 6b. fit the quantity to the delta band. Sizing answers "how much can
        # the budget carry" without ever seeing delta, and the gate answers
        # yes/no on whatever arrives — so a qty of 2 that breached the band
        # killed the whole trade instead of becoming the 1 that fits. On
        # 31 Aug that binary refused the session's best signals at qty=2 and
        # -$48k of delta when qty=1 at -$24k was inside the band. The band
        # itself does not move here; it binds gradually instead of all at once,
        # exactly as the heat and drawdown tapers already do.
        qty = sizing.qty
        book_greeks = self.data.post_trade_greeks(structure, 0)
        one_greeks = self.data.post_trade_greeks(structure, 1)
        per_spread_delta = one_greeks.delta_dollars - book_greeks.delta_dollars
        band = self.cfg.risk.delta_dollars_band
        if per_spread_delta:
            while qty >= 1 and abs(book_greeks.delta_dollars + per_spread_delta * qty) > band:
                qty -= 1
            if qty < 1:
                return self._drop(
                    "delta_fit",
                    item,
                    f"one spread carries ${per_spread_delta:,.0f} of delta against "
                    f"${band - abs(book_greeks.delta_dollars):,.0f} of remaining band",
                    signal_id=signal_id,
                )
            if qty < sizing.qty:
                self.audit.append(
                    "delta_fit",
                    {
                        "signal_id": signal_id,
                        "requested_qty": sizing.qty,
                        "fitted_qty": qty,
                        "per_spread_delta_dollars": per_spread_delta,
                        "book_delta_dollars": book_greeks.delta_dollars,
                    },
                )

        # 7. the gate — deterministic, non-bypassable
        blackout = self.corporate_blackout(item.symbol, structure, view.horizon_hours)
        ctx = GateContext(
            structure=structure,
            qty=qty,
            max_loss_per_spread=risk,
            meta_label_p=p,
            equity=market.equity,
            daily_pnl_pct=market.daily_pnl_pct,
            drawdown_pct=market.drawdown_pct,
            market_open=market.is_open,
            minutes_since_open=market.minutes_since_open,
            minutes_to_close=market.minutes_to_close,
            hours_to_expiry=hours_to_expiry,
            horizon_hours=view.horizon_hours,
            halted=is_halted(self.store),
            kill_switch=self.data.kill_switch(),
            portfolio=portfolio,
            post_trade_greeks=self.data.post_trade_greeks(structure, qty),
            correlations=self.data.correlations(),
            spread_pct_of_mid=spread_pct,
            open_interest=oi,
            orders_last_minute=market.orders_last_minute,
            new_positions_today=market.new_positions_today,
            duplicate_open=self.has_duplicate(structure),
            corporate_blackout=blackout,
            macro_window=macro_window,
            now=self.clock(),
        )
        decision = evaluate(ctx, self.cfg)
        self.audit.append(
            "gate",
            {
                "signal_id": signal_id,
                "structure": structure_key(structure),
                "qty": qty,
                "max_loss": risk * qty,
                "sizing_reason": sizing.reason,
                **decision.as_dict(),
            },
        )
        if not decision.approved:
            # Deferral, not discard, for the opening-auction window alone. The
            # check's intent is "not yet", but a discarded signal was "not
            # ever": _seen_news blocks reprocessing, so a story arriving at
            # minute 11 was never looked at again at minute 16. On 31 Aug the
            # session's two strongest signals (ratios 1.97 and 1.74) were lost
            # exactly this way. A signal refused ONLY on market_window during
            # the opening skip is queued and re-entered once the window opens,
            # reusing this analyst view (a second call could change the
            # opinion); staleness is enforced again at retry, and every other
            # stage re-runs in full.
            failed = [c for c in decision.checks if not c.passed]
            in_opening = market.minutes_since_open < self.cfg.gate.skip_first_minutes
            if (
                in_opening
                and failed
                and all(c.name == "market_window" for c in failed)
                and not getattr(self, "_floor_mode", False)
                and len(self._deferred) < 10
            ):
                self._deferred[signal_id] = (item, view)
                self.audit.append(
                    "signal_deferred",
                    {
                        "signal_id": signal_id,
                        "symbol": item.symbol,
                        "minutes_since_open": market.minutes_since_open,
                        "reason": decision.reason,
                    },
                )
                return Outcome("deferred", False, f"opening auction — retry after "
                               f"{self.cfg.gate.skip_first_minutes}m: {decision.reason}",
                               signal_id)
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
            qty=qty,
            entry_price=net_price,
            max_loss=risk * qty,
            status="opening",
            horizon_hours=effective_horizon,
            opened_at=self.clock().isoformat(),
            # The regime the bandit will be rewarded against when this closes,
            # the features the meta-labeler will train on, and the thesis the
            # completion and contradiction checks evaluate. All of it has to be
            # captured now: none of it can be reconstructed after the fact.
            regime=str(regime),
            features_json=json.dumps(features.as_dict()),
            thesis_direction=str(view.direction),
            thesis_move_pct=float(view.expected_move_pct),
            entry_spot=float(spot),
        )
        self.router.submit_structure(structure, qty, net_price, coid, position_id)
        return Outcome(
            "executed",
            True,
            f"{kind} x{qty} at {net_price:+.2f} (risk ${risk * qty:,.0f})",
            signal_id,
        )

    # -- position management ---------------------------------------------
    # -- thesis invalidation ----------------------------------------------
    def check_contradiction(self, symbol: str, view) -> list[str]:
        """Close open positions whose thesis this news reverses.

        The bar is deliberately higher than the entry bar. An unrelated story
        reading mildly bearish must not churn a position, so only a confident
        and material reversal counts, and only against a directional thesis — a
        vol_only view predicts magnitude without a sign and cannot be reversed
        by direction.
        """
        cfg = self.cfg.manage
        if not cfg.exit_on_contradiction or view.direction not in ("up", "down"):
            return []
        if (
            view.confidence < cfg.contradiction_min_confidence
            or view.materiality < cfg.contradiction_min_materiality
        ):
            return []

        opposite = "down" if view.direction == "up" else "up"
        closed = []
        for row in self.store.open_positions_for(symbol):
            if row["thesis_direction"] != opposite:
                continue
            try:
                position = self._position_view(row)
            except (KeyError, ValueError) as e:
                self.audit.append(
                    "contradiction_error",
                    {"position_id": row["position_id"], "error": f"{type(e).__name__}: {e}"},
                )
                continue

            pnl = position.unrealized_pnl
            decision = ManageDecision(
                Action.CLOSE,
                Barrier.THESIS_BROKEN,
                f"news reverses the {opposite} thesis: {view.direction} at "
                f"confidence {view.confidence:.2f}, materiality {view.materiality:.2f}",
                pnl,
                1 if pnl > 0 else 0,
            )
            self.audit.append(
                "thesis_broken",
                {
                    "position_id": position.position_id,
                    "symbol": symbol,
                    "held_thesis": opposite,
                    "new_direction": view.direction,
                    "confidence": view.confidence,
                    "materiality": view.materiality,
                    "unrealized_pnl": pnl,
                },
            )
            self._close(row, position, decision, self.clock())
            closed.append(position.position_id)
        return closed

    def manage_positions(
        self, now: datetime, deadline: datetime | None = None, market=None
    ) -> list[Outcome]:
        """Walk every open position through the triple barrier."""
        outcomes = []
        macro_window = self.macro_window()
        at_bell = (
            market is not None
            and market.is_open
            and market.minutes_to_close <= self.cfg.manage.bell_buffer_minutes
        )
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
            bell = self._bell_context(row, now, market) if at_bell else None
            decision = evaluate_position(view, self.cfg, now, deadline, macro_window, bell)
            self.audit.append(
                "manage",
                {
                    "position_id": view.position_id,
                    "action": str(decision.action),
                    "barrier": str(decision.barrier),
                    "reason": decision.reason,
                    "unrealized_pnl": decision.unrealized_pnl,
                    "label": decision.label,
                    "at_bell": at_bell,
                    # The spot the thesis barriers judged against. Without it a
                    # recorded P&L path cannot replay thesis_complete — five of
                    # the first week's eight closes were unreplayable for lack
                    # of this one number.
                    "current_spot": view.current_spot,
                    "entry_spot": view.entry_spot,
                },
            )
            if decision.action is Action.CLOSE:
                self._close(row, view, decision, now)
                outcomes.append(Outcome("manage", False, decision.reason, view.position_id))
        return outcomes

    def _windowed_spread(self, structure, chain, snapshot_pct: float) -> float:
        """The structure's spread on a rolling median per leg, not an instant.

        The indicative feed swings tens of percent minute to minute (UBER,
        2 Sep: 30.8% at 13:38, under 20% at 13:49 and 15:55, unfillable at
        all three). Each chain fetch records every leg's spread; the gate
        judges the median over the window once enough observations exist,
        and the snapshot until then. Worst leg wins, as before.
        """
        from statistics import median

        window = self.cfg.gate.liquidity_window_minutes * 60
        min_obs = self.cfg.gate.liquidity_min_observations
        now = self.clock()
        by_symbol = {c.symbol: c for c in chain}
        worst = 0.0
        for leg in structure.legs:
            quote = by_symbol.get(leg.symbol)
            if quote is None:
                continue
            hist = self._spread_history.setdefault(leg.symbol, [])
            hist.append((now, float(quote.spread_pct_of_mid)))
            recent = [v for t, v in hist if (now - t).total_seconds() <= window]
            hist[:] = [(t, v) for t, v in hist if (now - t).total_seconds() <= window]
            leg_pct = median(recent) if len(recent) >= min_obs else float(quote.spread_pct_of_mid)
            worst = max(worst, leg_pct)
        return worst if worst > 0 else snapshot_pct

    def _bell_context(self, row, now: datetime, market):
        """What the bell gate needs to decide whether this position may carry."""
        from zoneinfo import ZoneInfo

        from glassbox import macro as macro_module
        from glassbox.manage import BellContext

        weekday = now.astimezone(ZoneInfo("America/New_York")).weekday()
        try:
            release = macro_module.release_before_next_open(self.cfg, now)
        except Exception:  # noqa: BLE001 -- a malformed calendar must not stop the gate
            release = None
        blocked = ""
        try:
            structure = self._structure_from_row(row)
            hours_to_next_open = (
                macro_module.next_session_open(now) - now
            ).total_seconds() / 3600 + 1
            result = self.corporate_blackout(row["underlying"], structure, hours_to_next_open)
            if result is not None and getattr(result, "blocked", False):
                blocked = result.detail
        except Exception:  # noqa: BLE001 -- an unavailable feed leaves this check unperformed
            blocked = ""
        return BellContext(
            at_bell=True,
            weekday_et=weekday,
            equity=float(market.equity),
            macro_before_open=release,
            corporate_blocked=blocked,
        )

    def heartbeat(self) -> None:
        """The supervisor flattens the book if this stops advancing."""
        self.store.set_state("trader_heartbeat", self.clock().isoformat())

    # -- overridable hooks (the ML layer plugs in here) -------------------
    def select_structure(self, eligible, regime=None):
        """Which structure expresses the view. Thompson sampling when a bandit
        is attached, otherwise the edge test's first choice."""
        if self.bandit is None or regime is None:
            return eligible[0], "no bandit; edge test's first choice"
        choice = self.bandit.select(eligible, regime)
        return choice.kind, choice.detail

    def effective_horizon(self, horizon_hours: float, market) -> float:
        """The horizon the trade manager will actually use.

        An intraday thesis is truncated to the close: the news jump it was
        formed on resolves inside the session, and holding past the bell leaves
        an expired thesis carrying overnight gap risk with nobody managing it.
        A multi-day thesis is left alone — spanning sessions is its purpose.
        """
        if horizon_hours > self.cfg.gate.intraday_horizon_hours:
            return horizon_hours
        # Truncate to the close minus a buffer, not the close itself: the
        # manager only runs while the market is open, so a horizon landing
        # exactly on the bell can never be observed elapsed (TLT, 1 Sep).
        buffer_hours = self.cfg.manage.bell_buffer_minutes / 60
        hours_to_close = max(0.0, market.minutes_to_close / 60 - buffer_hours)
        return min(horizon_hours, hours_to_close) if hours_to_close else horizon_hours

    # -- the daily best-idea floor ----------------------------------------
    def _record_floor_candidate(self, item, view, edge) -> None:
        """Remember the day's best band-refused signal.

        Score is the ratio's distance from 1.0 — how strongly the idea leans,
        in either direction. Pure agreement with the market is not an idea.
        """
        distance = abs(edge.ratio - 1.0)
        if distance < self.cfg.floor.min_ratio_distance:
            return
        best = getattr(self, "_floor_candidate", None)
        today = self.clock().date().isoformat()
        if best and best["day"] == today and best["distance"] >= distance:
            return
        self._floor_candidate = {
            "day": today,
            "distance": distance,
            "item": item,
            "view": view,
            "ratio": edge.ratio,
        }
        self.audit.append(
            "floor_candidate",
            {
                "signal_id": f"{item.symbol}-{item.id}",
                "symbol": item.symbol,
                "ratio": edge.ratio,
                "distance": round(distance, 3),
                "headline": item.headline[:160],
            },
        )

    def retry_deferred(self, market) -> list[Outcome]:
        """Re-enter signals parked by the opening-auction window, once each.

        Runs from the management tick. Each deferred signal gets exactly one
        retry: it is popped before processing, so an outcome — traded, vetoed,
        or dropped — is final. Staleness is enforced here because the retry
        reuses the saved analyst view and therefore skips the filter that
        would normally enforce it; the window skip must not become a loophole
        for trading old news.
        """
        if not self._deferred or not market.is_open:
            return []
        if market.minutes_since_open < self.cfg.gate.skip_first_minutes:
            return []  # still inside the window; keep waiting
        outcomes = []
        max_age = self.cfg.signal.max_news_age_hours * 3600
        for signal_id in list(self._deferred):
            item, view = self._deferred.pop(signal_id)
            age = (self.clock() - item.created_at).total_seconds()
            if age > max_age:
                outcomes.append(
                    self._drop(
                        "deferred_expired",
                        item,
                        f"stale by {age / 3600:.1f}h at retry",
                        signal_id=signal_id,
                    )
                )
                continue
            outcomes.append(self.process_news(item, market, view_override=view))
        return outcomes

    def maybe_floor_trade(self, market) -> Outcome | None:
        """Express the day's best idea at reduced size, once, late in the day.

        Runs from the management tick. Every condition is a plain guard so the
        audit trail shows exactly why a floor trade did or did not happen.
        """

        from glassbox.clock import MARKET_TZ

        if not self.cfg.floor.enabled or not market.is_open:
            return None
        candidate = getattr(self, "_floor_candidate", None)
        today = self.clock().date().isoformat()
        if not candidate or candidate["day"] != today:
            return None
        if self.store.get_state("floor_trade_date") == today:
            return None  # once per day, ever
        if self.store.positions_opened_on(today) > 0:
            return None  # organic flow already traded; the floor stands down

        # A floor attempt that sizes to zero — stacked regime and macro
        # haircuts can do that — legitimately fails and is retried later, since
        # conditions change. Retrying every sixty-second tick would run the full
        # pipeline a hundred times an afternoon and bury the audit log.
        last = self.store.get_state("floor_attempt_at")
        if last:
            from datetime import datetime as _dt

            age = (self.clock() - _dt.fromisoformat(last)).total_seconds() / 60
            if age < self.cfg.floor.retry_after_minutes:
                return None

        hour, minute = (int(x) for x in self.cfg.floor.after_time_et.split(":"))
        now_et = self.clock().astimezone(MARKET_TZ)
        if (now_et.hour, now_et.minute) < (hour, minute):
            return None

        item, view = candidate["item"], candidate["view"]
        self.audit.append(
            "floor_trigger",
            {
                "symbol": item.symbol,
                "ratio": candidate["ratio"],
                "reason": f"no organic trade by {self.cfg.floor.after_time_et} ET",
            },
        )
        # Re-enter the ordinary pipeline with the band relaxed and size halved.
        # Everything else — VRP, sizing caps, all 18 gate checks — applies in
        # full. The floor relaxes exactly one bar.
        self.store.set_state("floor_attempt_at", self.clock().isoformat())
        self._floor_mode = True
        try:
            outcome = self.process_news(self._floor_item(item), market, view_override=view)
        finally:
            self._floor_mode = False
        if outcome.traded:
            self.store.set_state("floor_trade_date", today)
        self.audit.append(
            "floor_outcome",
            {
                "symbol": item.symbol,
                "traded": outcome.traded,
                "stage": outcome.stage,
                "reason": outcome.reason[:200],
            },
        )
        return outcome

    @staticmethod
    def _floor_item(item):
        """A fresh identity so dedup and idempotency treat this as a distinct
        decision, clearly labelled in every downstream record."""
        from dataclasses import replace

        return replace(item, id=f"{item.id}-floor")

    def regime_reading(self):
        """Current market regime, or None when the data layer cannot supply it.
        Cached for ten minutes — the regime does not change per news item."""
        if not hasattr(self.data, "daily_closes"):
            return None
        from glassbox import regime as regime_module

        try:
            return self.data._cached(
                ("regime",),
                600.0,
                lambda: regime_module.compute(self.data, self.filter.universe, self.cfg),
            )
        except Exception:  # noqa: BLE001 -- unknown environment is reported as
            # unknown; it must never block the pipeline.
            return None

    def macro_window(self):
        from glassbox import macro as macro_module

        try:
            return macro_module.current_window(self.cfg, self.clock())
        except Exception:  # noqa: BLE001
            return None

    def corporate_blackout(self, symbol: str, structure, horizon_hours: float):
        """Upcoming corporate actions for the underlying, or None if unchecked.

        Returning None rather than an empty result matters: the gate reports
        "not checked" instead of implying the security was cleared.
        """
        if not hasattr(self.data, "corporate_events"):
            return None
        from datetime import timedelta

        from glassbox.data.corporate import blackout

        try:
            events = self.data.corporate_events(symbol)
        except Exception:  # noqa: BLE001 -- an unavailable feed leaves the check
            # unperformed and honestly labelled, rather than falsely passing.
            return None
        horizon_end = (self.clock() + timedelta(hours=horizon_hours)).date()
        return blackout(events, horizon_end, is_credit=structure.is_credit)

    def meta_label(self, features, fallback: float) -> tuple[float, str]:
        """P(this signal is profitable). The meta-labeler abstains below its
        minimum sample count, in which case the analyst's own confidence is
        used — a number we actually have rather than one we made up."""
        if self.meta_labeler is None:
            return fallback, "no meta-labeler; using analyst confidence"
        return self.meta_labeler.predict(features, fallback)

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

        from glassbox.execution.router import legs_as_dicts

        return json.dumps(legs_as_dicts(structure))

    def _structure_from_row(self, row):
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
            thesis_direction=row["thesis_direction"] or "",
            thesis_move_pct=float(row["thesis_move_pct"] or 0.0),
            entry_spot=float(row["entry_spot"] or 0.0),
            current_spot=self._safe_spot(row["underlying"]),
        )

    def _safe_spot(self, symbol: str) -> float:
        """Spot for the completion check. Unavailable means zero, which the
        check reads as "cannot evaluate" rather than "no movement"."""
        try:
            return float(self.data.spot(symbol))
        except (ValueError, KeyError):
            return 0.0

    @staticmethod
    def _with_peak(view: PositionView, peak: float) -> PositionView:
        from dataclasses import replace

        return replace(view, peak_pnl=peak)

    def _close(self, row, view: PositionView, decision, now: datetime) -> None:
        """Submit the closing order. The position is *not* declared closed here.

        A close that has been submitted is not yet a close that has happened:
        recording it as done while the order rests would tell the book it is
        flat while the broker still holds the legs. The lifecycle confirms the
        fill, realises P&L from the actual price, and only then feeds the
        learners — the loop closes on what happened, not on what we expected.
        """
        from glassbox.execution.ids import close_order_id

        structure = self._structure_from_row(row)
        # The attempt counter makes each retry after a dead close a fresh
        # client_order_id — Alpaca never accepts a reused id, even from a
        # canceled order, so resubmitting the same one would reject forever.
        attempt = int(self.store.get_state(f"close_attempt:{view.position_id}") or 0)
        coid = close_order_id(view.position_id, str(decision.barrier), attempt)
        self.store.upsert_position(view.position_id, status="closing")
        self.store.set_state(f"close_barrier:{view.position_id}", str(decision.barrier))
        # The close limit is the NEGATION of the position's current value.
        # Prices in this codebase are entry-oriented (positive = the spread's
        # value to us), but Alpaca's MLEG limit is order-oriented: positive =
        # net debit we pay, negative = net credit we must receive. Closing a
        # debit spread means selling it, so a +2.47 value becomes a -2.47 limit
        # ("pay me at least 2.47"); closing a credit spread means buying it
        # back, so a -1.20 value becomes a +1.20 limit ("I'll pay up to 1.20").
        # Submitting the entry-signed value instead made debit-spread closes
        # uncontrolled market-chasers and credit-spread closes impossible —
        # a limit demanding we RECEIVE money to buy back a short spread.
        self.router.submit_structure(
            structure, view.qty, round(-view.current_price, 2), coid, view.position_id,
            closing=True,
        )

    def feed_learners(self, row, label: int) -> None:
        """Reward the bandit for a realised outcome. Called by the lifecycle
        once the closing fill is confirmed.

        Worth recording: before the lifecycle existed this call existed nowhere
        in the live path at all — only the drills rewarded the bandit, so in
        live trading it would have sampled untouched priors all week while the
        dashboard showed it as active.
        """
        if self.bandit is not None and row["regime"]:
            from glassbox.ml.bandit import VolRegime

            self.bandit.update(row["kind"], VolRegime(row["regime"]), won=bool(label))
