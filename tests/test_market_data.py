"""Market data adapter tests. The OCC parser and the pricing paths are what
would silently corrupt everything downstream if wrong."""

from datetime import date

import pytest

from glassbox.data.market import parse_occ
from glassbox.structures import Right


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("AAPL260918C00230000", ("AAPL", date(2026, 9, 18), Right.CALL, 230.0)),
        ("SPY260903P00420000", ("SPY", date(2026, 9, 3), Right.PUT, 420.0)),
        ("T261016C00002500", ("T", date(2026, 10, 16), Right.CALL, 2.5)),
        ("GOOGL270115P01750500", ("GOOGL", date(2027, 1, 15), Right.PUT, 1750.5)),
    ],
)
def test_occ_symbols_decode(symbol, expected):
    assert parse_occ(symbol) == expected


@pytest.mark.parametrize("bad", ["AAPL", "NOTASYMBOL", "AAPL260918X00230000", ""])
def test_malformed_occ_rejected(bad):
    """A misparsed strike would corrupt every risk calculation downstream."""
    with pytest.raises(ValueError, match="not an OCC"):
        parse_occ(bad)


def test_structure_price_refuses_to_default_a_missing_quote(store):
    """A structure priced at zero would read as a costless exit and mislead
    every barrier in the trade manager."""
    from glassbox.data.market import MarketData
    from glassbox.structures import Leg, LegSide, Structure, StructureKind

    md = MarketData(
        trading_client=None,
        stock_client=None,
        option_client=None,
        store=store,
        root=None,
    )
    md._cache[("snap", "SPY", date(2026, 9, 18))] = type(
        "C", (), {"value": {}, "at": __import__("glassbox.clock", fromlist=["x"]).now_utc()}
    )()
    structure = Structure(
        StructureKind.BULL_PUT_SPREAD,
        "SPY",
        (
            Leg("SPY260918P00440000", Right.PUT, 440, date(2026, 9, 18), LegSide.SHORT),
            Leg("SPY260918P00435000", Right.PUT, 435, date(2026, 9, 18), LegSide.LONG),
        ),
    )
    with pytest.raises(ValueError, match="no quote"):
        md.structure_price(structure)


def test_structure_price_marks_at_the_liquidation_side(store):
    """COIN, 1 Sep: the mid said 0.48 to close, the market wanted 0.58, and
    the break-even barrier acted on a peak that never existed. A mark is the
    price we would actually get: long legs sold at the bid, short legs bought
    back at the ask — never the mid."""
    from glassbox.data.market import MarketData
    from glassbox.structures import Leg, LegSide, Structure, StructureKind

    md = MarketData(
        trading_client=None, stock_client=None, option_client=None, store=store, root=None
    )

    def snap(bid, ask):
        return type("S", (), {"latest_quote": type("Q", (), {"bid_price": bid, "ask_price": ask})()})()

    md._cache[("snap", "SPY", date(2026, 9, 18))] = type(
        "C",
        (),
        {
            "value": {
                "SPY260918P00440000": snap(1.00, 1.20),  # short leg: buy back at 1.20
                "SPY260918P00435000": snap(0.40, 0.60),  # long leg: sell at 0.40
            },
            "at": __import__("glassbox.clock", fromlist=["x"]).now_utc(),
        },
    )()
    structure = Structure(
        StructureKind.BULL_PUT_SPREAD,
        "SPY",
        (
            Leg("SPY260918P00440000", Right.PUT, 440, date(2026, 9, 18), LegSide.SHORT),
            Leg("SPY260918P00435000", Right.PUT, 435, date(2026, 9, 18), LegSide.LONG),
        ),
    )
    # liquidation: +0.40 (sell long) - 1.20 (buy back short) = -0.80
    # the mid would have said +0.50 - 1.10 = -0.60 — 25% rosier
    assert md.structure_price(structure) == pytest.approx(-0.80)


def test_realized_vol_returns_none_rather_than_guessing(store, monkeypatch):
    """Sizing skips the volatility budget when vol is unknown; inventing a
    number would silently change position size."""
    from glassbox.data.market import MarketData

    md = MarketData(
        trading_client=None,
        stock_client=None,
        option_client=None,
        store=store,
        root=None,
    )
    monkeypatch.setattr(md, "daily_closes", lambda symbol, days=60: [100.0, 101.0])
    assert md.realized_vol("SPY") is None


def test_realized_vol_computes_from_closes(store, monkeypatch):
    from glassbox.data.market import MarketData

    md = MarketData(
        trading_client=None,
        stock_client=None,
        option_client=None,
        store=store,
        root=None,
    )
    closes = [100.0 * (1.01 if i % 2 else 0.99) ** 1 for i in range(30)]
    monkeypatch.setattr(md, "daily_closes", lambda symbol, days=60: closes)
    vol = md.realized_vol("SPY")
    assert vol is not None and 0.0 < vol < 1.0


def test_cache_avoids_refetching_within_ttl(store):
    from glassbox.data.market import MarketData

    md = MarketData(
        trading_client=None,
        stock_client=None,
        option_client=None,
        store=store,
        root=None,
    )
    calls = []

    def produce():
        calls.append(1)
        return "value"

    assert md._cached(("k",), 60.0, produce) == "value"
    assert md._cached(("k",), 60.0, produce) == "value"
    assert len(calls) == 1, "a burst of news must not refetch the same chain"


# --- spot pricing: the one-sided quote bug --------------------------------


class FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price, self.ask_price = bid, ask


class FakeTrade:
    def __init__(self, price):
        self.price = price


class FakeStockClient:
    def __init__(self, quote=None, trade=None):
        self._quote, self._trade = quote, trade

    def get_stock_latest_quote(self, req):
        if self._quote is None:
            raise KeyError("no quote")
        return {"AAPL": self._quote}

    def get_stock_latest_trade(self, req):
        if self._trade is None:
            raise KeyError("no trade")
        return {"AAPL": self._trade}


def _md(store, **kw):
    from glassbox.data.market import MarketData

    return MarketData(
        trading_client=None,
        stock_client=FakeStockClient(**kw),
        option_client=None,
        store=store,
        root=None,
    )


def test_one_sided_quote_falls_back_to_last_trade(store):
    """Observed outside market hours: bid=294.98, ask=0.00 has a midpoint of
    147.49 for a stock trading at 314.54. Using that would pick strikes 150
    points away and corrupt every downstream risk number."""
    md = _md(store, quote=FakeQuote(294.98, 0.0), trade=FakeTrade(314.54))
    assert md.spot("AAPL") == pytest.approx(314.54)


def test_tight_two_sided_quote_is_preferred(store):
    md = _md(store, quote=FakeQuote(314.50, 314.60), trade=FakeTrade(314.00))
    assert md.spot("AAPL") == pytest.approx(314.55)


def test_absurdly_wide_quote_is_rejected(store):
    """A 5%-wide market is not a price."""
    md = _md(store, quote=FakeQuote(300.00, 330.00), trade=FakeTrade(314.54))
    assert md.spot("AAPL") == pytest.approx(314.54)


def test_quote_disagreeing_with_last_print_is_distrusted(store):
    """One of them is stale; the actual transaction wins."""
    md = _md(store, quote=FakeQuote(200.00, 200.10), trade=FakeTrade(314.54))
    assert md.spot("AAPL") == pytest.approx(314.54)


def test_quote_used_when_no_trade_available(store):
    md = _md(store, quote=FakeQuote(314.50, 314.60), trade=None)
    assert md.spot("AAPL") == pytest.approx(314.55)


def test_no_usable_price_raises_rather_than_guessing(store):
    md = _md(store, quote=FakeQuote(0.0, 0.0), trade=None)
    with pytest.raises(ValueError, match="no usable price"):
        md.spot("AAPL")


def test_chain_capture_writes_quotes_for_replay(store, tmp_path):
    """The audit records decisions, not the chain they were made against.
    Without capture a session cannot be replayed to ask whether a change
    would have fired (0 records carried quotes before 2 Sep)."""
    import json as _json

    from glassbox.data.market import MarketData

    md = MarketData(
        trading_client=None, stock_client=None, option_client=None, store=store,
        root=None, chain_capture_dir=tmp_path,
    )
    from glassbox.chain import ContractQuote

    quotes = [
        ContractQuote("SPY260918P00440000", Right.PUT, 440.0, date(2026, 9, 18), 1.0, 1.2, 500),
    ]
    md._capture_chain("SPY", date(2026, 9, 18), 445.0, quotes)
    files = list(tmp_path.glob("*-chains.jsonl"))
    assert len(files) == 1
    rec = _json.loads(files[0].read_text().strip())
    assert rec["symbol"] == "SPY" and rec["spot"] == 445.0
    assert rec["quotes"][0]["oi"] == 500 and rec["quotes"][0]["bid"] == 1.0


def test_chain_capture_failure_never_breaks_pricing(store):
    """A full disk must not stop trading."""
    from glassbox.data.market import MarketData

    md = MarketData(
        trading_client=None, stock_client=None, option_client=None, store=store,
        root=None, chain_capture_dir="/nonexistent/\0/bad",
    )
    md._capture_chain("SPY", date(2026, 9, 18), 445.0, [])  # must not raise
