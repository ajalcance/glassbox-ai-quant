"""Live market data adapter.

Implements the interface the orchestrator depends on, backed by Alpaca. Every
call is cached with a short TTL: a burst of news on the same symbol should not
re-fetch an option chain five times, and Alpaca's documented intermittent
latency is easier to absorb when we are not asking needlessly.

Quotes come from option snapshots (bid/ask, IV, Greeks) and static contract data
comes from the contracts endpoint (strike, expiry, open interest). They are
merged on the OCC symbol rather than fetched twice.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise

from glassbox.chain import ContractQuote
from glassbox.clock import MARKET_TZ, market_date, now_utc, parse_expiry
from glassbox.portfolio import Greeks
from glassbox.structures import LegSide, Right, Structure

OCC = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


def parse_occ(symbol: str) -> tuple[str, date, Right, float]:
    """Decode an OCC option symbol: AAPL260918C00230000."""
    m = OCC.match(symbol)
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    ymd = m.group("ymd")
    return (
        m.group("root"),
        date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])),
        Right.CALL if m.group("right") == "C" else Right.PUT,
        int(m.group("strike")) / 1000,
    )


@dataclass
class _Cached:
    value: object
    at: datetime


@dataclass
class MarketData:
    trading_client: object
    stock_client: object
    option_client: object
    store: object
    root: object  # repo root, for the kill-switch file
    quote_ttl: float = 20.0  # seconds
    bar_ttl: float = 3600.0
    models_dir: object = None
    _cache: dict = field(default_factory=dict)
    _harrv: object = None

    def __post_init__(self):
        from pathlib import Path

        if self.models_dir is None:
            self.models_dir = Path(__file__).resolve().parents[2] / "models"

    # -- caching ----------------------------------------------------------
    def _cached(self, key, ttl: float, produce):
        hit = self._cache.get(key)
        if hit and (now_utc() - hit.at).total_seconds() < ttl:
            return hit.value
        value = produce()
        self._cache[key] = _Cached(value, now_utc())
        return value

    # -- underlying -------------------------------------------------------
    def spot(self, symbol: str) -> float:
        """Underlying price, from a quote only when the quote is trustworthy.

        Outside regular hours Alpaca returns one-sided quotes — a real observed
        case was bid=294.98 with ask=0.00, whose midpoint is 147.49 for a stock
        trading at 314.54. A spot that wrong is not a small error: it selects
        strikes 150 points away, becomes the denominator of the implied-move
        calculation, and scales every delta-dollar figure the gate checks. So a
        quote is used only when both sides are present and the spread is sane,
        and the last traded price is the fallback.
        """

        def fetch():
            from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

            quote_mid = None
            try:
                q = self.stock_client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=symbol)
                )[symbol]
                bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
                if bid > 0 and ask > 0 and ask >= bid:
                    mid = (bid + ask) / 2
                    if (ask - bid) / mid <= 0.05:  # a 5%-wide market is not a price
                        quote_mid = mid
            except (KeyError, TypeError, ValueError):
                quote_mid = None

            trade_price = None
            try:
                t = self.stock_client.get_stock_latest_trade(
                    StockLatestTradeRequest(symbol_or_symbols=symbol)
                )[symbol]
                trade_price = float(t.price) if t.price else None
            except (KeyError, TypeError, ValueError):
                trade_price = None

            # Both available: prefer the quote, but distrust it if it disagrees
            # materially with the last print — one of them is stale or broken.
            if quote_mid and trade_price:
                if abs(quote_mid - trade_price) / trade_price > 0.10:
                    return trade_price
                return quote_mid
            price = quote_mid or trade_price
            if not price or price <= 0:
                raise ValueError(f"no usable price for {symbol}")
            return price

        return self._cached(("spot", symbol), self.quote_ttl, fetch)

    def daily_closes(self, symbol: str, days: int = 60) -> list[float]:
        def fetch():
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            bars = self.stock_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Day,
                    start=now_utc() - timedelta(days=days * 2),
                )
            )
            return [float(b.close) for b in bars.data.get(symbol, [])]

        return self._cached(("closes", symbol, days), self.bar_ttl, fetch)

    def realized_vol(self, symbol: str) -> float | None:
        """Daily realised volatility as a decimal, or None if unknowable.

        Returning None rather than a guess matters: sizing skips the volatility
        budget when vol is unavailable instead of inventing a number.
        """
        import math

        closes = self.daily_closes(symbol, 30)
        if len(closes) < 10:
            return None
        returns = [math.log(b / a) for a, b in pairwise(closes) if a > 0]
        return statistics.stdev(returns) if len(returns) > 2 else None

    def correlations(self) -> dict[tuple[str, str], float]:
        """Pairwise correlation across currently held underlyings.

        Only names we actually hold matter — the gate asks whether a candidate
        duplicates existing exposure, not how the whole universe co-moves.
        """
        held = sorted({r["underlying"] for r in self.store.open_positions()})
        if len(held) < 1:
            return {}

        def fetch():
            import math

            series = {}
            for sym in held:
                closes = self.daily_closes(sym, 60)
                if len(closes) >= 20:
                    series[sym] = [math.log(b / a) for a, b in pairwise(closes) if a > 0]
            out = {}
            names = sorted(series)
            for i, a in enumerate(names):
                for b in names[i + 1 :]:
                    n = min(len(series[a]), len(series[b]))
                    if n >= 20:
                        out[(a, b)] = statistics.correlation(series[a][-n:], series[b][-n:])
            return out

        return self._cached(("corr", tuple(held)), self.bar_ttl, fetch)

    # -- options ----------------------------------------------------------
    def _contracts(self, symbol: str, expiry_lo: date, expiry_hi: date):
        def fetch():
            from alpaca.trading.requests import GetOptionContractsRequest

            out, token = [], None
            for _ in range(4):  # bounded pagination
                page = self.trading_client.get_option_contracts(
                    GetOptionContractsRequest(
                        underlying_symbols=[symbol],
                        expiration_date_gte=expiry_lo.isoformat(),
                        expiration_date_lte=expiry_hi.isoformat(),
                        limit=1000,
                        page_token=token,
                    )
                )
                out.extend(page.option_contracts or [])
                token = getattr(page, "next_page_token", None)
                if not token:
                    break
            return out

        return self._cached(("contracts", symbol, expiry_lo, expiry_hi), self.bar_ttl, fetch)

    def chain(self, symbol: str, horizon_hours: float) -> list[ContractQuote]:
        """Quotes for a single expiry that comfortably spans the horizon.

        One expiry only: mixing expiries is how a calendar spread sneaks in, and
        the defined-risk invariant rejects those anyway.
        """
        today = market_date()
        # Never the front expiry inside our minimum time-to-expiry rule.
        lo = today + timedelta(days=max(3, int(horizon_hours // 24) + 1))
        hi = lo + timedelta(days=30)
        contracts = [c for c in self._contracts(symbol, lo, hi) if c.tradable]
        if not contracts:
            return []

        by_expiry: dict[date, list] = {}
        for c in contracts:
            by_expiry.setdefault(parse_expiry(c.expiration_date), []).append(c)
        expiry = min(by_expiry)
        chosen = by_expiry[expiry]

        spot = self.spot(symbol)
        # Only strikes near the money are usable; fetching the full chain wastes
        # a large request on contracts no structure would ever select.
        near = [c for c in chosen if abs(float(c.strike_price) - spot) <= spot * 0.15]
        snapshots = self._snapshots(symbol, expiry)

        out = []
        for c in near:
            snap = snapshots.get(c.symbol)
            quote = getattr(snap, "latest_quote", None) if snap else None
            if not quote or quote.bid_price is None or quote.ask_price is None:
                continue
            _, exp, right, strike = parse_occ(c.symbol)
            out.append(
                ContractQuote(
                    symbol=c.symbol,
                    right=right,
                    strike=strike,
                    expiry=exp,
                    bid=float(quote.bid_price),
                    ask=float(quote.ask_price),
                    open_interest=int(c.open_interest or 0),
                    implied_volatility=getattr(snap, "implied_volatility", None),
                    delta=self._greek(snap, "delta"),
                )
            )
        return out

    def _snapshots(self, symbol: str, expiry: date) -> dict:
        def fetch():
            from alpaca.data.requests import OptionChainRequest

            return self.option_client.get_option_chain(
                OptionChainRequest(underlying_symbol=symbol, expiration_date=expiry)
            )

        return self._cached(("snap", symbol, expiry), self.quote_ttl, fetch)

    @staticmethod
    def _greek(snapshot, name: str) -> float | None:
        greeks = getattr(snapshot, "greeks", None)
        return getattr(greeks, name, None) if greeks else None

    def hours_to_expiry(self, chain: list[ContractQuote]) -> float:
        if not chain:
            return 0.0
        expiry = min(c.expiry for c in chain)
        close = datetime.combine(expiry, datetime.min.time(), MARKET_TZ).replace(hour=16)
        return max(0.0, (close - now_utc()).total_seconds() / 3600)

    def structure_hours_to_expiry(self, structure: Structure) -> float:
        close = datetime.combine(structure.expiry, datetime.min.time(), MARKET_TZ).replace(hour=16)
        return max(0.0, (close - now_utc()).total_seconds() / 3600)

    def structure_price(self, structure: Structure) -> float:
        """Net mid to close, in the router's sign convention.

        A missing quote raises rather than defaulting to zero — a structure
        priced at zero would read as a costless exit and mislead every barrier.
        """
        snaps = self._snapshots(structure.underlying, structure.expiry)
        net = 0.0
        for leg in structure.legs:
            snap = snaps.get(leg.symbol)
            quote = getattr(snap, "latest_quote", None) if snap else None
            if not quote or quote.bid_price is None or quote.ask_price is None:
                raise ValueError(f"no quote for {leg.symbol}; cannot price structure")
            mid = (float(quote.bid_price) + float(quote.ask_price)) / 2
            net += mid * leg.ratio_qty * (1 if leg.side is LegSide.LONG else -1)
        return round(net, 2)

    def post_trade_greeks(self, structure: Structure, qty: int) -> Greeks:
        """Book Greeks including the candidate, in delta-dollars."""
        total = self._book_greeks()
        spot = self.spot(structure.underlying)
        snaps = self._snapshots(structure.underlying, structure.expiry)
        for leg in structure.legs:
            delta = self._greek(snaps.get(leg.symbol), "delta")
            if delta is None:
                continue
            sign = 1 if leg.side is LegSide.LONG else -1
            total = total + Greeks(
                delta_dollars=float(delta) * sign * leg.ratio_qty * qty * 100 * spot
            )
        return total

    def _book_greeks(self) -> Greeks:
        import json

        total = Greeks()
        for row in self.store.open_positions():
            try:
                spot = self.spot(row["underlying"])
                legs = json.loads(row["legs_json"])
                expiry = parse_expiry(legs[0]["expiry"])
                snaps = self._snapshots(row["underlying"], expiry)
            except (ValueError, KeyError, IndexError):
                continue  # a position we cannot price does not silently vanish
                # from risk — it is simply not added; reconciliation catches drift
            for leg in legs:
                delta = self._greek(snaps.get(leg["symbol"]), "delta")
                if delta is None:
                    continue
                sign = 1 if leg["side"] == "long" else -1
                total = total + Greeks(
                    delta_dollars=float(delta)
                    * sign
                    * leg["ratio_qty"]
                    * int(row["qty"])
                    * 100
                    * spot
                )
        return total

    def move_since(self, symbol: str, since) -> float | None:
        """Absolute percentage move in the underlying since a point in time.

        Used to discount an expected move by however much of it the market has
        already made. Direction is deliberately ignored: a stock that moved
        hard *against* the analyst's thesis has not left the move available — it
        has produced evidence the thesis is wrong. Either way the opportunity is
        smaller than the raw estimate suggests.

        Returns None when it cannot be measured, which the caller treats as "no
        adjustment" rather than "no move".
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        try:
            bars = self.stock_client.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=since)
            ).data.get(symbol, [])
        except Exception:  # noqa: BLE001 -- unmeasurable is not zero
            return None
        if not bars:
            return None

        reference = float(bars[0].open)
        if reference <= 0:
            return None
        try:
            now = self.spot(symbol)
        except ValueError:
            return None
        return abs(now - reference) / reference * 100

    def measure_move(self, symbol: str, start, end) -> tuple[float, float] | None:
        """Signed and absolute percentage move between two past times.

        Used to score a prediction after its horizon has elapsed, so it reads
        bars across the window rather than comparing against the live quote —
        the job may run well after the horizon closed.
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        try:
            bars = self.stock_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Minute,
                    start=start,
                    end=end,
                )
            ).data.get(symbol, [])
        except Exception:  # noqa: BLE001 -- unmeasurable is not zero
            return None
        if len(bars) < 2:
            return None
        first, last = float(bars[0].open), float(bars[-1].close)
        if first <= 0:
            return None
        signed = (last - first) / first * 100
        return signed, abs(signed)

    def forecast_move_pct(self, symbol: str, hours_to_expiry: float) -> float | None:
        """The move the volatility model expects over the option's life.

        Gives the frozen HAR-RV model a second job: not just scaling position
        size, but answering whether the options are pricing more movement than
        this underlying historically delivers.
        """
        import math

        from glassbox.ml.volforecast import HarRv

        if self._harrv is None:
            self._harrv = HarRv.load(self.models_dir / "harrv.json")
        if not self._harrv.is_trained:
            return None
        closes = self.daily_closes(symbol, 60)
        daily = self._harrv.forecast(closes)
        if not daily or hours_to_expiry <= 0:
            return None
        # Daily volatility scales with the square root of time. This *is* a
        # diffusive quantity, unlike the event jump, so the scaling is correct.
        days = hours_to_expiry / 24
        return daily * math.sqrt(max(days, 1.0)) * 100

    # -- corporate actions ------------------------------------------------
    def corporate_events(self, symbol: str) -> list:
        """Upcoming actions for a symbol, cached for an hour — the set does not
        change minute to minute and this runs on every candidate signal."""
        from glassbox.data.corporate import fetch_events

        return self._cached(
            ("corp", symbol), self.bar_ttl, lambda: fetch_events(self.trading_client, symbol)
        )

    def session(self):
        """Exchange session times for today, or None when unavailable."""
        from glassbox.data.calendar import fetch_session

        return self._cached(
            ("session", market_date()),
            self.bar_ttl,
            lambda: fetch_session(self.trading_client),
        )

    # -- session ----------------------------------------------------------
    def kill_switch(self) -> bool:
        from glassbox.supervisor.guards import KILL_SWITCH_FILE

        return (self.root / KILL_SWITCH_FILE).exists()

    def clock(self):
        return self._cached(("clock",), 30.0, self.trading_client.get_clock)
