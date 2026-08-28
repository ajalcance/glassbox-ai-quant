from datetime import UTC, datetime, timedelta

from glassbox.config import load_config
from glassbox.signal.filter import NewsFilter, NewsItem, jaccard, tokenise

CFG = load_config().signal
NOW = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)
UNIVERSE = {"AAPL", "SPY", "NVDA"}


def item(headline, symbol="AAPL", summary="", minutes_ago=0, id_="n1"):
    return NewsItem(
        id=id_,
        symbol=symbol,
        headline=headline,
        summary=summary,
        source="benzinga",
        created_at=NOW - timedelta(minutes=minutes_ago),
    )


def make_filter():
    return NewsFilter(UNIVERSE, CFG)


def test_accepts_novel_material_news():
    r = make_filter().evaluate(item("Apple beats on earnings, raises guidance"), now=NOW)
    assert r.passed, r.reason


def test_rejects_symbol_outside_universe():
    r = make_filter().evaluate(item("Big news", symbol="ZZZZ"), now=NOW)
    assert not r.passed and "universe" in r.reason


def test_rejects_boilerplate():
    for headline in (
        "Market Update: Stocks Moving In Tuesday's Session",
        "Top Gainers And Losers Today",
        "Unusual Options Activity Detected In Apple",
    ):
        r = make_filter().evaluate(item(headline), now=NOW)
        assert not r.passed, f"should have dropped: {headline}"


def test_rejects_stale_news():
    r = make_filter().evaluate(item("Apple announces major acquisition", minutes_ago=200), now=NOW)
    assert not r.passed and "stale" in r.reason


def test_rejects_restatement_of_recent_story():
    """The 40th retelling of the same story is not a new signal."""
    f = make_filter()
    first = f.evaluate(item("Apple beats on earnings and raises full year guidance"), now=NOW)
    assert first.passed

    repeat = f.evaluate(
        item("Apple beats on earnings, raises full year guidance", id_="n2"), now=NOW
    )
    assert not repeat.passed and "restatement" in repeat.reason


def test_genuinely_different_news_on_same_symbol_passes():
    f = make_filter()
    assert f.evaluate(item("Apple beats on earnings and raises guidance"), now=NOW).passed
    other = f.evaluate(
        item("Apple faces antitrust lawsuit from European Commission", id_="n2"), now=NOW
    )
    assert other.passed, other.reason


def test_novelty_is_per_symbol():
    f = make_filter()
    headline = "Company beats on earnings and raises full year guidance"
    assert f.evaluate(item(headline, symbol="AAPL"), now=NOW).passed
    assert f.evaluate(item(headline, symbol="NVDA", id_="n2"), now=NOW).passed


def test_similarity_helpers():
    assert jaccard(tokenise("apple beats earnings"), tokenise("apple beats earnings")) == 1.0
    assert jaccard(tokenise("apple earnings"), tokenise("nvidia lawsuit")) == 0.0
    assert "the" not in tokenise("the apple")
