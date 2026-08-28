"""End-to-end pipeline tests with stubbed dependencies.

These are the tests that would have caught a wiring bug at 3am. They exercise
the whole path — filter, analyst, market data, edge test, chain, sizing, gate,
execution — without touching the network.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from glassbox.chain import ContractQuote
from glassbox.config import load_config
from glassbox.portfolio import Greeks
from glassbox.signal.analyst import AnalystView
from glassbox.signal.filter import NewsFilter, NewsItem
from glassbox.structures import Right
from glassbox.trader import MarketState, Trader

CFG = load_config()
NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
EXPIRY = date(2026, 9, 18)
SPOT = 230.0


def make_chain(spot=SPOT, oi=5000, spread=0.02):
    """A realistic chain: strikes every 5 points, tight markets.

    Time value decays with distance from the money. A flat time value would
    make every vertical spread cost exactly zero, which the pricing-plausibility
    guard correctly rejects — real chains are not shaped that way.
    """
    out = []
    for strike in range(int(spot) - 40, int(spot) + 45, 5):
        for right in (Right.CALL, Right.PUT):
            intrinsic = max(0.0, (spot - strike) if right is Right.CALL else (strike - spot))
            time_value = max(0.05, 3.0 - 0.08 * abs(strike - spot))
            mid = intrinsic + time_value
            out.append(
                ContractQuote(
                    symbol=f"AAPL{EXPIRY:%y%m%d}{right[0].upper()}{int(strike * 1000):08d}",
                    right=right,
                    strike=float(strike),
                    expiry=EXPIRY,
                    bid=mid - spread,
                    ask=mid + spread,
                    open_interest=oi,
                )
            )
    return out


DEFAULT_GREEKS = Greeks(delta_dollars=1_000)


class StubData:
    def __init__(self, chain=None, vol=0.015, greeks=None, kill=False):
        self._chain = chain if chain is not None else make_chain()
        self._vol = vol
        self._greeks = greeks or DEFAULT_GREEKS
        self._kill = kill
        self.price = 0.0  # tests set this to move a position

    def spot(self, symbol):
        return SPOT

    def chain(self, symbol, horizon_hours):
        return self._chain

    def hours_to_expiry(self, chain):
        return 400.0

    def realized_vol(self, symbol):
        return self._vol

    def kill_switch(self):
        return self._kill

    def correlations(self):
        return {}

    def post_trade_greeks(self, structure, qty):
        return self._greeks

    def structure_price(self, structure):
        return self.price

    def structure_hours_to_expiry(self, structure):
        return 400.0


class StubLlm:
    def __init__(self, view=None, raises=None):
        self.view = view or AnalystView(
            event_type="earnings",
            direction="up",
            confidence=0.85,
            # The stub chain implies a 2.61% move, so the default view must
            # expect meaningfully more than that to be a real signal.
            expected_move_pct=4.0,
            horizon_hours=48.0,
            materiality=0.9,
            rationale="Beat and raised.",
        )
        self.raises = raises

    def extract(self, **kwargs):
        if self.raises:
            raise self.raises
        return self.view


class StubRouter:
    def __init__(self):
        self.submitted = []

    def submit_structure(self, structure, qty, price, coid, position_id, closing=False):
        self.submitted.append(
            {
                "structure": structure,
                "qty": qty,
                "price": price,
                "coid": coid,
                "position_id": position_id,
                "closing": closing,
            }
        )
        return type("O", (), {"id": f"alp-{len(self.submitted)}"})()


def make_trader(store, audit, *, llm=None, data=None, router=None):
    return Trader(
        cfg=CFG,
        store=store,
        audit=audit,
        router=router or StubRouter(),
        news_filter=NewsFilter({"AAPL"}, CFG.signal),
        llm=llm or StubLlm(),
        market_data=data or StubData(),
        clock=lambda: NOW,
    )


def news(headline="Apple beats Q3 estimates and raises full-year guidance", **kw):
    base = {
        "id": "n1",
        "symbol": "AAPL",
        "headline": headline,
        "summary": "Strong iPhone demand drove the beat.",
        "source": "benzinga",
        "created_at": NOW,
    }
    base.update(kw)
    return NewsItem(**base)


def market(**kw):
    base = {
        "is_open": True,
        "minutes_since_open": 60,
        "minutes_to_close": 180,
        "equity": 100_000.0,
        "daily_pnl_pct": 0.0,
        "drawdown_pct": 0.0,
    }
    base.update(kw)
    return MarketState(**base)


# --- the happy path -------------------------------------------------------


def test_full_pipeline_places_an_order(store, audit):
    router = StubRouter()
    t = make_trader(store, audit, router=router)
    outcome = t.process_news(news(), market())
    assert outcome.traded, outcome.reason
    assert len(router.submitted) == 1
    assert store.open_positions(), "position must be recorded before submission"


def test_every_stage_is_audited(store, audit):
    t = make_trader(store, audit)
    t.process_news(news(), market())
    kinds = [r["kind"] for r in _audit_records(audit)]
    assert "analyst_view" in kinds and "edge_test" in kinds and "gate" in kinds


def _audit_records(audit):
    import json

    path = next(iter(sorted(audit.dir.glob("*.jsonl"))))
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- every exit stage -----------------------------------------------------


def test_filter_rejects_before_any_model_cost(store, audit):
    llm = StubLlm()
    t = make_trader(store, audit, llm=llm)
    out = t.process_news(news(headline="Market Update: Stocks Moving Today"), market())
    assert not out.traded and out.stage == "filter"


def test_symbol_outside_universe_dropped(store, audit):
    t = make_trader(store, audit)
    out = t.process_news(news(symbol="TSLA"), market())
    assert not out.traded and out.stage == "filter"


def test_analyst_failure_drops_the_event(store, audit):
    from glassbox.llm import LlmUnavailableError

    t = make_trader(store, audit, llm=StubLlm(raises=LlmUnavailableError("503")))
    out = t.process_news(news(), market())
    assert not out.traded and out.stage == "analyst"


def test_fairly_priced_news_stops_at_the_edge_test(store, audit):
    """A 3.0 straddle on a 230 stock implies ~2.6%; expecting 2.6% is no edge."""
    view = AnalystView(
        event_type="earnings",
        direction="up",
        confidence=0.85,
        expected_move_pct=2.6,
        horizon_hours=48.0,
        materiality=0.9,
        rationale="x",
    )
    router = StubRouter()
    t = make_trader(store, audit, llm=StubLlm(view), router=router)
    out = t.process_news(news(), market())
    assert not out.traded and out.stage == "edge"
    assert router.submitted == []


def test_gate_veto_blocks_execution(store, audit):
    router = StubRouter()
    t = make_trader(store, audit, router=router, data=StubData(kill=True))
    out = t.process_news(news(), market())
    assert not out.traded and out.stage == "gate"
    assert router.submitted == [], "a vetoed trade must never reach the router"


def test_closed_market_blocks_execution(store, audit):
    router = StubRouter()
    t = make_trader(store, audit, router=router)
    out = t.process_news(news(), market(is_open=False))
    assert not out.traded and router.submitted == []


def test_illiquid_chain_blocks_execution(store, audit):
    router = StubRouter()
    t = make_trader(store, audit, router=router, data=StubData(chain=make_chain(oi=5)))
    out = t.process_news(news(), market())
    assert not out.traded and router.submitted == []


def test_halted_system_refuses_before_anything_else(store, audit):
    from glassbox.reconcile import HALT_KEY

    store.set_state(HALT_KEY, "reconciliation mismatch")
    router = StubRouter()
    t = make_trader(store, audit, router=router)
    out = t.process_news(news(), market())
    assert not out.traded and out.stage == "halt"
    assert router.submitted == []


# --- position management ---------------------------------------------------


def _open_a_position(store, audit, router=None):
    t = make_trader(store, audit, router=router)
    outcome = t.process_news(news(), market())
    assert outcome.traded, outcome.reason
    row = store.open_positions()[0]
    store.upsert_position(row["position_id"], status="open")
    # Price the structure at its entry so P&L is flat unless a test moves it.
    t.data.price = float(row["entry_price"])
    return t, row


def test_manage_holds_a_position_between_barriers(store, audit):
    t, _row = _open_a_position(store, audit)
    assert t.manage_positions(NOW + timedelta(hours=1)) == []
    assert store.open_positions(), "position should still be open"


def test_deadline_closes_and_labels(store, audit):
    t, _row = _open_a_position(store, audit)
    outcomes = t.manage_positions(NOW + timedelta(hours=1), deadline=NOW)
    assert len(outcomes) == 1
    assert store.training_rows(), "a closed position must produce a training row"
    assert store.open_positions() == []


def test_close_submits_an_inverted_order(store, audit):
    router = StubRouter()
    t = Trader(
        cfg=CFG,
        store=store,
        audit=audit,
        router=router,
        news_filter=NewsFilter({"AAPL"}, CFG.signal),
        llm=StubLlm(),
        market_data=StubData(),
        clock=lambda: NOW,
    )
    t.process_news(news(), market())
    store.upsert_position(store.open_positions()[0]["position_id"], status="open")
    t.manage_positions(NOW + timedelta(hours=1), deadline=NOW)
    assert router.submitted[-1]["closing"] is True


def test_heartbeat_is_written_for_the_supervisor(store, audit):
    t = make_trader(store, audit)
    t.heartbeat()
    assert store.get_state("trader_heartbeat") == NOW.isoformat()


# --- hooks the ML layer will replace --------------------------------------


def test_meta_label_defaults_to_analyst_confidence(store, audit):
    """With no meta-labeler attached, the honest stand-in is the analyst's own
    confidence — a number we actually have, not an invented one."""
    t = make_trader(store, audit)
    p, detail = t.meta_label(features=None, fallback=0.85)
    assert p == pytest.approx(0.85)
    assert "no meta-labeler" in detail


def test_structure_selection_is_overridable(store, audit):
    """The bandit plugs in here."""
    from glassbox.structures import StructureKind

    class BanditTrader(Trader):
        def select_structure(self, eligible, regime=None):
            return eligible[-1], "overridden"

    t = BanditTrader(
        cfg=CFG,
        store=store,
        audit=audit,
        router=StubRouter(),
        news_filter=NewsFilter({"AAPL"}, CFG.signal),
        llm=StubLlm(),
        market_data=StubData(),
        clock=lambda: NOW,
    )
    kind, detail = t.select_structure([StructureKind.IRON_CONDOR])
    assert kind is StructureKind.IRON_CONDOR and detail == "overridden"


def test_closed_market_skips_the_model_entirely(store, audit):
    """News arrives around the clock. Without an early exit the system would
    spend every night paying a model to read stories it cannot trade."""

    class CountingLlm(StubLlm):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def extract(self, **kwargs):
            self.calls += 1
            return super().extract(**kwargs)

    llm = CountingLlm()
    t = make_trader(store, audit, llm=llm)
    out = t.process_news(news(), market(is_open=False))
    assert not out.traded and out.stage == "market_closed"
    assert llm.calls == 0, "analyst was called on news we could not act on"


# --- time-of-day handling -------------------------------------------------


def test_intraday_horizon_truncates_to_the_close(store, audit):
    """A four-hour thesis with two hours left becomes a two-hour thesis, not an
    eighteen-hour overnight hold on a view that expired at the bell."""
    t = make_trader(store, audit)
    assert t.effective_horizon(4.0, market(minutes_to_close=120)) == pytest.approx(2.0)


def test_intraday_horizon_left_alone_when_the_session_is_long_enough(store, audit):
    t = make_trader(store, audit)
    assert t.effective_horizon(4.0, market(minutes_to_close=300)) == pytest.approx(4.0)


def test_multi_day_horizon_is_not_truncated(store, audit):
    """Spanning sessions is what a multi-day thesis is for."""
    t = make_trader(store, audit)
    assert t.effective_horizon(48.0, market(minutes_to_close=60)) == pytest.approx(48.0)


def test_stored_horizon_is_the_truncated_one(store, audit):
    """The trade manager reads this value, so truncation has to reach the store."""
    t = make_trader(store, audit)
    outcome = t.process_news(news(), market(minutes_to_close=180))
    assert outcome.traded, outcome.reason
    row = store.open_positions()[0]
    # The stub analyst states 48h, which is multi-day and passes through intact.
    assert float(row["horizon_hours"]) == pytest.approx(48.0)


def test_intraday_thesis_is_stored_truncated_end_to_end(store, audit):
    """The whole point: an intraday view entered late must be closed at the bell,
    and the trade manager only knows that through the stored horizon."""
    view = AnalystView(
        event_type="earnings",
        direction="up",
        confidence=0.85,
        expected_move_pct=4.0,
        horizon_hours=6.0,  # intraday
        materiality=0.9,
        rationale="x",
    )
    t = make_trader(store, audit, llm=StubLlm(view))
    outcome = t.process_news(news(), market(minutes_to_close=200))
    assert outcome.traded, outcome.reason
    row = store.open_positions()[0]
    assert float(row["horizon_hours"]) == pytest.approx(200 / 60), (
        "a 6h thesis with 3h20m of session left must be stored as 3h20m"
    )


def test_intraday_thesis_without_room_is_vetoed_not_truncated(store, audit):
    """Truncating a 6h thesis to 30 minutes would be a different trade. Below the
    session-room floor we decline instead."""
    view = AnalystView(
        event_type="earnings",
        direction="up",
        confidence=0.85,
        expected_move_pct=4.0,
        horizon_hours=6.0,
        materiality=0.9,
        rationale="x",
    )
    router = StubRouter()
    t = make_trader(store, audit, llm=StubLlm(view), router=router)
    outcome = t.process_news(news(), market(minutes_to_close=30))
    assert not outcome.traded and outcome.stage == "gate"
    assert "session_room" in outcome.reason
    assert router.submitted == []


# --- the daily best-idea floor --------------------------------------------


def fairly_priced_view(expected=2.9):
    """The stub chain implies ~2.61%, so ~2.9% sits inside the band (ratio ~1.1)
    — refused organically, but leaning enough to be a floor candidate."""
    return AnalystView(
        event_type="earnings",
        direction="up",
        confidence=0.85,
        expected_move_pct=expected,
        horizon_hours=48.0,
        materiality=0.9,
        rationale="x",
    )


class AdvancingClock:
    """Morning while the news is processed, afternoon when the floor fires —
    the same shape as a real session."""

    def __init__(self, at=None):
        self.at = at or NOW  # NOW is 15:00 UTC = 11:00 ET, fresh news

    def __call__(self):
        return self.at

    def afternoon(self):
        """18:30 UTC = 14:30 ET — past the 14:00 floor trigger."""
        self.at = datetime(2026, 9, 1, 18, 30, tzinfo=UTC)


def make_floor_trader(store, audit, router=None, clock=None):
    return Trader(
        cfg=CFG,
        store=store,
        audit=audit,
        router=router or StubRouter(),
        news_filter=NewsFilter({"AAPL"}, CFG.signal),
        llm=StubLlm(fairly_priced_view()),
        market_data=StubData(),
        clock=clock or AdvancingClock(),
    )


def test_band_refusal_becomes_a_floor_candidate(store, audit):
    t = make_floor_trader(store, audit)
    out = t.process_news(news(), market())
    assert not out.traded and out.stage == "edge"
    assert t._floor_candidate["item"].symbol == "AAPL"


def test_pure_agreement_is_not_an_idea(store, audit):
    """Ratio within min_ratio_distance of 1.0 is a coin flip, not a best idea."""
    t = make_floor_trader(store, audit)
    t.llm = StubLlm(fairly_priced_view(expected=2.65))  # ratio ~1.01
    t.process_news(news(), market())
    assert getattr(t, "_floor_candidate", None) is None


def test_floor_trades_the_candidate_at_reduced_size(store, audit):
    router = StubRouter()
    t = make_floor_trader(store, audit, router=router)
    t.process_news(news(), market())
    assert router.submitted == []

    t.clock.afternoon()
    outcome = t.maybe_floor_trade(market())
    assert outcome is not None and outcome.traded, outcome and outcome.reason
    assert len(router.submitted) == 1
    assert store.get_state("floor_trade_date") == "2026-09-01"


def test_floor_fires_once_per_day(store, audit):
    router = StubRouter()
    t = make_floor_trader(store, audit, router=router)
    t.process_news(news(), market())
    t.clock.afternoon()
    first = t.maybe_floor_trade(market())
    assert first is not None and first.traded
    # Candidate for the same day again; the floor must stand down.
    t.llm = StubLlm(fairly_priced_view())
    assert t.maybe_floor_trade(market()) is None


def test_floor_stands_down_when_organic_flow_traded(store, audit):
    router = StubRouter()
    t = make_floor_trader(store, audit, router=router)
    t.process_news(news(), market())
    t.clock.afternoon()
    # An organic position opened today.
    store.upsert_position(
        "pos-organic",
        underlying="AAPL",
        kind="call_debit_spread",
        legs_json="[]",
        qty=1,
        max_loss=100.0,
        status="open",
        opened_at=t.clock().isoformat(),
    )
    assert t.maybe_floor_trade(market()) is None


def test_floor_waits_for_the_trigger_time(store, audit):
    t = make_floor_trader(store, audit)  # clock stays at 11:00 ET
    t.process_news(news(), market())
    assert t.maybe_floor_trade(market()) is None


def test_floor_respects_the_gate(store, audit):
    """The floor relaxes one bar. A kill switch still refuses it."""
    router = StubRouter()
    t = make_floor_trader(store, audit, router=router)
    t.data._kill = True
    t.process_news(news(), market())
    t.clock.afternoon()
    outcome = t.maybe_floor_trade(market())
    assert outcome is not None and not outcome.traded
    assert router.submitted == []
    assert store.get_state("floor_trade_date") is None, (
        "a refused floor attempt must not consume the day's one shot"
    )


def test_floor_disabled_is_inert(store, audit, monkeypatch):
    t = make_floor_trader(store, audit)
    t.process_news(news(), market())
    t.clock.afternoon()
    monkeypatch.setattr(CFG.floor, "enabled", False)
    try:
        assert t.maybe_floor_trade(market()) is None
    finally:
        monkeypatch.setattr(CFG.floor, "enabled", True)


def test_floor_size_is_halved(store, audit):
    """The floor's audit record shows the context multiplier including 0.5x."""
    import json as _json

    t = make_floor_trader(store, audit)
    t.process_news(news(), market())
    t.clock.afternoon()
    t.maybe_floor_trade(market())
    records = _audit_records(audit)
    ml = [r for r in records if r["kind"] == "ml" and "floor" in r.get("context_detail", "")]
    assert ml, "floor trade must carry the floor size factor in its ml record"
    assert ml[-1]["context_multiplier"] <= 0.5 + 1e-9
    assert _json  # keep import used


def test_floor_reentry_does_not_double_count_the_prediction(store, audit):
    """A floor re-entry replays a view already recorded this morning. Recording
    it twice would count one estimate twice and bias calibration toward whatever
    the floor happened to pick."""
    t = make_floor_trader(store, audit)
    t.process_news(news(), market())
    before = len(store.due_predictions("2099-01-01T00:00:00+00:00", limit=500))
    assert before == 1

    t.clock.afternoon()
    t.maybe_floor_trade(market())
    after = len(store.due_predictions("2099-01-01T00:00:00+00:00", limit=500))
    assert after == before, "floor re-entry recorded a duplicate prediction"


def test_failed_floor_attempt_backs_off_before_retrying(store, audit):
    """Stacked haircuts can size a floor trade to zero. That legitimately fails
    and is retried later — but not on every sixty-second tick, which would run
    the pipeline a hundred times an afternoon and bury the audit log."""
    router = StubRouter()
    t = make_floor_trader(store, audit, router=router)
    t.data._kill = True  # forces the gate to refuse
    t.process_news(news(), market())
    t.clock.afternoon()

    first = t.maybe_floor_trade(market())
    assert first is not None and not first.traded
    assert store.get_state("floor_trade_date") is None

    # Immediately after, the attempt is skipped entirely.
    assert t.maybe_floor_trade(market()) is None

    # Once the back-off elapses it tries again, and succeeds when conditions clear.
    t.clock.at = t.clock.at + timedelta(minutes=CFG.floor.retry_after_minutes + 1)
    t.data._kill = False
    retried = t.maybe_floor_trade(market())
    assert retried is not None and retried.traded
