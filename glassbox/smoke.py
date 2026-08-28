"""Day-1 smoke test: auth, market clock, data, news, options chain, audit log.

    uv run python -m glassbox.smoke

Read-only — places no orders. Exits non-zero on the first failure."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from glassbox.audit import AuditLog
from glassbox.config import load_config


def check(label: str, fn):
    try:
        detail = fn()
        print(f"  ok    {label}: {detail}")
        return True
    except Exception as e:  # noqa: BLE001 — smoke test reports everything
        print(f"  FAIL  {label}: {type(e).__name__}: {e}")
        return False


def main() -> int:
    cfg = load_config()
    print(f"GlassBox smoke test — paper={cfg.account.paper}")
    results = []

    def auth():
        from glassbox.data.alpaca_client import trading_client

        acct = trading_client().get_account()
        return f"account {acct.account_number} status={acct.status} equity=${acct.equity}"

    def clock():
        from glassbox.data.alpaca_client import trading_client

        c = trading_client().get_clock()
        state = "OPEN" if c.is_open else "closed"
        return f"market {state}, next_open={c.next_open:%Y-%m-%d %H:%M %Z}"

    def bars():
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        from glassbox.data.alpaca_client import stock_data_client

        start = datetime.now(UTC) - timedelta(days=5)
        req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day, start=start)
        data = stock_data_client().get_stock_bars(req)
        n = len(data["SPY"])
        return f"{n} daily SPY bars, last close={data['SPY'][-1].close}"

    def news():
        from alpaca.data.requests import NewsRequest

        from glassbox.data.alpaca_client import news_client

        req = NewsRequest(symbols="AAPL", limit=3)
        items = news_client().get_news(req).data["news"]
        return f"{len(items)} AAPL stories, latest: {items[0].headline[:60]!r}"

    def chain():
        from alpaca.trading.requests import GetOptionContractsRequest

        from glassbox.data.alpaca_client import trading_client

        req = GetOptionContractsRequest(underlying_symbols=["SPY"], limit=5)
        contracts = trading_client().get_option_contracts(req).option_contracts
        return f"{len(contracts)} SPY contracts, e.g. {contracts[0].symbol}"

    def audit():
        log = AuditLog(cfg.paths.audit_dir)
        rec = log.append("smoke", {"note": "day-1 smoke test"})
        ok, n = AuditLog.verify_chain(log.dir / f"{rec['ts'][:10]}.jsonl")
        assert ok, "audit chain verification failed"
        return f"chain verified, {n} records today"

    for label, fn in [
        ("alpaca auth", auth),
        ("market clock", clock),
        ("stock bars", bars),
        ("news api", news),
        ("options chain", chain),
        ("audit log", audit),
    ]:
        results.append(check(label, fn))

    if all(results):
        print("ALL CHECKS PASSED")
        return 0
    print(f"{results.count(False)} check(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
