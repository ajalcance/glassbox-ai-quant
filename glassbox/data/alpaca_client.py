"""Thin factories for Alpaca clients. Broker is truth; these are the only
places credentials are read. Supervisor uses its own credentials — never share
a session between trader and supervisor."""

from __future__ import annotations

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.trading.client import TradingClient

from glassbox.config import require_env


def trading_client() -> TradingClient:
    return TradingClient(
        api_key=require_env("ALPACA_API_KEY_ID"),
        secret_key=require_env("ALPACA_API_SECRET_KEY"),
        paper=True,  # hard-pinned: this codebase never touches live endpoints
    )


def supervisor_trading_client() -> TradingClient:
    return TradingClient(
        api_key=require_env("SUPERVISOR_ALPACA_API_KEY_ID"),
        secret_key=require_env("SUPERVISOR_ALPACA_API_SECRET_KEY"),
        paper=True,
    )


def stock_data_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=require_env("ALPACA_API_KEY_ID"),
        secret_key=require_env("ALPACA_API_SECRET_KEY"),
    )


def option_data_client() -> OptionHistoricalDataClient:
    return OptionHistoricalDataClient(
        api_key=require_env("ALPACA_API_KEY_ID"),
        secret_key=require_env("ALPACA_API_SECRET_KEY"),
    )


def news_client() -> NewsClient:
    return NewsClient(
        api_key=require_env("ALPACA_API_KEY_ID"),
        secret_key=require_env("ALPACA_API_SECRET_KEY"),
    )
