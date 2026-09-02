"""The trader process — event-driven, unattended.

    uv run python -m glassbox.runner --dry-run     # decide, log, place nothing
    uv run python -m glassbox.runner               # live paper trading

News arrives on Alpaca's WebSocket. A polled REST fallback runs alongside it,
because a socket that dies quietly is worse than one that never connected: the
system would look healthy and simply stop seeing the market. The poller also
carries the management tick, so positions keep being managed even if no news
arrives at all.

Everything is wrapped so a single bad event cannot take the process down. The
supervisor is watching the heartbeat, and a trader that exits mid-position is
exactly the situation the guards exist to prevent.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from glassbox import preflight
from glassbox.audit import AuditLog
from glassbox.clock import MARKET_TZ, now_utc
from glassbox.config import load_config
from glassbox.data.alpaca_client import (
    news_client,
    option_data_client,
    stock_data_client,
    trading_client,
)
from glassbox.data.market import MarketData
from glassbox.execution.router import OrderRouter
from glassbox.ml.bandit import ThompsonBandit
from glassbox.ml.metalabel import MetaLabeler
from glassbox.reconcile import enforce, is_halted
from glassbox.signal.filter import NewsFilter, NewsItem
from glassbox.store import Store
from glassbox.supervisor.guards import session_baseline
from glassbox.trader import MarketState, Trader

ROOT = Path(__file__).resolve().parents[1]


class DryRunRouter:
    """Records what would have been sent. Used by --dry-run so the entire
    pipeline runs against live data with nothing reaching the broker."""

    def __init__(self, audit: AuditLog):
        self.audit = audit
        self.submitted = []

    def submit_structure(self, structure, qty, price, coid, position_id, closing=False):
        from glassbox.structures import structure_key

        record = {
            "structure": structure_key(structure),
            "qty": qty,
            "limit_price": price,
            "client_order_id": coid,
            "position_id": position_id,
            "closing": closing,
        }
        self.submitted.append(record)
        self.audit.append("dry_run_order", record)
        print(f"  DRY RUN would submit: {record['structure']} x{qty} @ {price:+.2f}")
        return type("DryOrder", (), {"id": f"dry-{len(self.submitted)}"})()

    def cancel(self, client_order_id, alpaca_order_id):
        self.audit.append("dry_run_cancel", {"client_order_id": client_order_id})

    def poll(self, client_order_id):
        """A dry-run order fills instantly at its limit, so the dry run
        exercises the entire fill lifecycle rather than skipping it."""
        for record in self.submitted:
            if record["client_order_id"] == client_order_id:
                return "filled", record["limit_price"]
        return "canceled", None


def build_universe(cfg) -> set[str]:
    """Liquid, optionable names. Deliberately small and static for the contest:
    a universe that changes underneath the agent is one more thing that can
    silently break overnight."""
    return {
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
        "AMD",
        "NFLX",
        "AVGO",
        "JPM",
        "BAC",
        "XOM",
        "CVX",
        "WMT",
        "COST",
        "UNH",
        "JNJ",
        "PFE",
        "DIS",
        "INTC",
        "MU",
        "CRM",
        "ORCL",
        "ADBE",
        "PYPL",
        "SHOP",
        "UBER",
        "COIN",
        "PLTR",
        "SMCI",
        "MRNA",
        "GS",
        "MS",
        "CAT",
        "BA",
        "GLD",
        "SLV",
        "TLT",
        "HYG",
        "XLE",
        "XLF",
        "XLK",
        "EEM",
        "FXI",
        "ARKK",
    }


class Runner:
    def __init__(self, cfg, *, dry_run: bool = False, poll_seconds: float = 60.0):
        self.cfg = cfg
        self.dry_run = dry_run
        self.poll_seconds = poll_seconds
        self.stopping = threading.Event()

        self.store = Store(cfg.paths.db)
        self.audit = AuditLog(cfg.paths.audit_dir, role="trader")
        self.trading = trading_client()
        self.data = MarketData(
            trading_client=self.trading,
            stock_client=stock_data_client(),
            option_client=option_data_client(),
            store=self.store,
            root=ROOT,
            # The audit log records what we decided; this records what we
            # decided against, so a session can be replayed to ask whether a
            # change would have fired. Only the live runner captures — drills
            # and the scheduler would pollute the record with synthetic reads.
            chain_capture_dir=ROOT / "chains",
        )
        router = (
            DryRunRouter(self.audit)
            if dry_run
            else OrderRouter(self.trading, self.store, self.audit)
        )
        models = Path(cfg.paths.models_dir)
        # A missing or stale model is not an error — it means abstain, and the
        # pipeline falls back to the analyst's own confidence.
        self.meta_labeler = MetaLabeler.load(
            models / "metalabel.pkl", min_samples=cfg.ml.min_training_samples
        )
        self.bandit = ThompsonBandit(
            self.store,
            prior_alpha=cfg.ml.bandit_prior_alpha,
            prior_beta=cfg.ml.bandit_prior_beta,
        )
        self.universe = build_universe(cfg)
        self.trader = Trader(
            cfg=cfg,
            store=self.store,
            audit=self.audit,
            router=router,
            news_filter=NewsFilter(self.universe, cfg.signal),
            llm=self._llm(),
            market_data=self.data,
            clock=now_utc,
            meta_labeler=self.meta_labeler,
            bandit=self.bandit,
        )
        self._seen_news: set[str] = set()
        self._deadline = self._parse_deadline(cfg.manage.flatten_all_at)

    @staticmethod
    def _llm():
        from glassbox.config import load_config as _load
        from glassbox.llm import LlmClient

        return LlmClient.from_config(_load())

    @staticmethod
    def _parse_deadline(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    # -- account snapshot -------------------------------------------------
    def market_state(self) -> MarketState:
        account = self.trading.get_account()
        equity = float(account.equity)
        clock = self.data.clock()

        start = session_baseline(self.store, equity)
        peak = max(float(self.store.get_state("peak_equity", "0") or 0), equity)

        now = now_utc().astimezone(MARKET_TZ)
        # Session times come from the exchange calendar. Assuming 09:30-16:00
        # is wrong on an early close by three hours, which would disable the
        # end-of-session guard exactly when liquidity is worst.
        session = self.data.session()
        if session is not None:
            since_open = session.minutes_since_open(now)
            to_close = session.minutes_to_close(now)
        else:
            open_at = now.replace(hour=9, minute=30, second=0, microsecond=0)
            close_at = now.replace(hour=16, minute=0, second=0, microsecond=0)
            since_open = max(0, int((now - open_at).total_seconds() // 60))
            to_close = max(0, int((close_at - now).total_seconds() // 60))

        return MarketState(
            is_open=bool(clock.is_open),
            minutes_since_open=since_open,
            minutes_to_close=to_close,
            equity=equity,
            daily_pnl_pct=100 * (equity - start) / start if start else 0.0,
            drawdown_pct=100 * (equity - peak) / peak if peak else 0.0,
            # Positions *opened today*, not positions currently open. Carrying
            # three overnight would otherwise consume three of the day's ten
            # slots without a single new order, while positions opened and
            # closed within the day would not count at all — wrong in both
            # directions.
            new_positions_today=self.store.positions_opened_on(
                now_utc().astimezone(MARKET_TZ).date().isoformat()
            ),
            orders_last_minute=self.store.orders_created_since(
                (now_utc() - timedelta(seconds=60)).isoformat()
            ),
            loss_streak=self.store.recent_loss_streak(),
        )

    # -- work -------------------------------------------------------------
    def handle_news(self, item: NewsItem) -> None:
        if item.id in self._seen_news:
            return  # the socket and the poller both see the same story
        self._seen_news.add(item.id)
        try:
            outcome = self.trader.process_news(item, self.market_state())
        except Exception as e:  # noqa: BLE001 -- one malformed story must never
            # end the session; the event is logged and the loop continues.
            self.audit.append(
                "pipeline_error",
                {"news_id": item.id, "symbol": item.symbol, "error": f"{type(e).__name__}: {e}"},
            )
            print(f"  error on {item.symbol}: {type(e).__name__}: {e}", file=sys.stderr)
            return
        marker = "TRADE" if outcome.traded else "  -  "
        print(f"[{now_utc():%H:%M:%S}] {marker} {item.symbol}: {outcome.reason[:110]}")

    def tick(self) -> None:
        """Management, reconciliation, heartbeat. Runs whether or not news came."""
        self.trader.heartbeat()
        try:
            from glassbox.execution import lifecycle

            for event in lifecycle.sync(self.trader, now_utc()):
                print(f"[{now_utc():%H:%M:%S}] {event}")
        except Exception as e:  # noqa: BLE001 -- a failed sync retries next tick
            self.audit.append("lifecycle_error", {"error": f"{type(e).__name__}: {e}"})
        try:
            enforce(self.store, self.audit, self.trading.get_all_positions())
        except Exception as e:  # noqa: BLE001 -- a failed reconcile must not kill
            # the loop; it will be retried, and the supervisor still guards equity.
            self.audit.append("reconcile_error", {"error": f"{type(e).__name__}: {e}"})
        if is_halted(self.store):
            print(f"[{now_utc():%H:%M:%S}] HALTED — {self.store.get_state('halt_reason')}")
            return
        market = self.market_state()
        # Manage only while the market is open: barriers act through close
        # orders, which cannot fill against a closed book, and overnight quote
        # snapshots would feed the view stale marks. Positions carried across
        # sessions (deliberate, 1 Sep) are re-evaluated on the first open tick
        # — a gap through the stop closes at the open, which is the earliest
        # any system could act on it. Lifecycle sync and reconcile above still
        # run around the clock; it is only barrier evaluation that sleeps.
        if market.is_open:
            for outcome in self.trader.manage_positions(now_utc(), self._deadline, market):
                print(f"[{now_utc():%H:%M:%S}] CLOSE {outcome.reason[:110]}")
        try:
            for outcome in self.trader.retry_deferred(market):
                marker = "TRADE" if outcome.traded else "retry"
                print(f"[{now_utc():%H:%M:%S}] {marker} deferred: {outcome.reason[:100]}")
        except Exception as e:  # noqa: BLE001 -- a failed retry pass must not
            # take the tick down; the deferred entries are already popped or
            # will expire on staleness.
            self.audit.append("deferred_retry_error", {"error": f"{type(e).__name__}: {e}"})
        try:
            floor = self.trader.maybe_floor_trade(self.market_state())
            if floor is not None:
                marker = "FLOOR TRADE" if floor.traded else "floor declined"
                print(f"[{now_utc():%H:%M:%S}] {marker}: {floor.reason[:100]}")
        except Exception as e:  # noqa: BLE001 -- the floor is a convenience; it
            # must never take the tick down with it.
            self.audit.append("floor_error", {"error": f"{type(e).__name__}: {e}"})

    def poll_news(self) -> None:
        """REST fallback. A silently dead socket looks exactly like a quiet
        market, so we never rely on the stream alone."""
        from alpaca.data.requests import NewsRequest

        try:
            response = news_client().get_news(
                NewsRequest(
                    symbols=",".join(sorted(self.universe)),
                    start=now_utc() - timedelta(minutes=15),
                    limit=50,
                )
            )
        except Exception as e:  # noqa: BLE001 -- transient data outage
            self.audit.append("news_poll_error", {"error": f"{type(e).__name__}: {e}"})
            return
        for raw in response.data.get("news", []):
            for symbol in raw.symbols:
                if symbol in self.universe:
                    self.handle_news(
                        NewsItem(
                            id=str(raw.id),
                            symbol=symbol,
                            headline=raw.headline,
                            summary=raw.summary or "",
                            source=raw.source or "alpaca",
                            created_at=raw.created_at,
                        )
                    )
                    break

    # -- lifecycle --------------------------------------------------------
    def run(self) -> int:
        mode = "DRY RUN (no orders)" if self.dry_run else "LIVE (paper account)"
        print(f"GlassBox trader starting — {mode}")
        print(f"  universe: {len(self.universe)} symbols")
        print(
            "  meta-labeler: "
            + (
                f"trained on {self.meta_labeler.n_samples} outcomes"
                if self.meta_labeler.is_trained
                else f"abstaining ({self.meta_labeler.n_samples}/"
                f"{self.cfg.ml.min_training_samples} samples) — using analyst confidence"
            )
        )
        arms = self.bandit.summary()
        print(f"  bandit: {len(arms)} arm/regime cells with history")
        print(f"  deadline: {self._deadline}")
        self.audit.append(
            "trader_start",
            {"dry_run": self.dry_run, "universe_size": len(self.universe)},
        )

        check = preflight.run(self.trading, self.data)
        for line in check.checks:
            print(line)
        self.audit.append("preflight", check.as_dict())
        if not check.ok:
            print("preflight failed — refusing to start", file=sys.stderr)
            return 2

        stream_thread = threading.Thread(target=self._run_stream, daemon=True)
        stream_thread.start()

        try:
            while not self.stopping.is_set():
                self.tick()
                self.poll_news()
                self.stopping.wait(self.poll_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            self.audit.append("trader_stop", {})
            self.store.close()
            print("trader stopped")
        return 0

    def _run_stream(self) -> None:
        """News websocket. Reconnects on failure; the poller covers the gap."""
        while not self.stopping.is_set():
            try:
                from alpaca.data.live.news import NewsDataStream

                from glassbox.config import require_env

                stream = NewsDataStream(
                    require_env("ALPACA_API_KEY_ID"), require_env("ALPACA_API_SECRET_KEY")
                )

                async def on_news(raw):
                    for symbol in getattr(raw, "symbols", []):
                        if symbol in self.universe:
                            self.handle_news(
                                NewsItem(
                                    id=str(raw.id),
                                    symbol=symbol,
                                    headline=raw.headline,
                                    summary=getattr(raw, "summary", "") or "",
                                    source=getattr(raw, "source", "alpaca") or "alpaca",
                                    created_at=raw.created_at,
                                )
                            )
                            break

                stream.subscribe_news(on_news, *sorted(self.universe))
                stream.run()
            except Exception as e:  # noqa: BLE001 -- reconnect rather than exit;
                # the REST poller keeps the system seeing news meanwhile.
                self.audit.append("stream_error", {"error": f"{type(e).__name__}: {e}"})
                self.stopping.wait(10)


def main() -> int:
    parser = argparse.ArgumentParser(description="GlassBox trader")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full pipeline against live data, place no orders",
    )
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--once", action="store_true", help="single tick then exit")
    args = parser.parse_args()

    runner = Runner(load_config(), dry_run=args.dry_run, poll_seconds=args.poll_seconds)

    if args.once:
        runner.tick()
        runner.poll_news()
        runner.store.close()
        return 0

    def stop(*_):
        runner.stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
