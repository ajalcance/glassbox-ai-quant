"""Thin factories for Alpaca clients. Broker is truth; these are the only
places credentials are read. Supervisor uses its own credentials — never share
a session between trader and supervisor."""

from __future__ import annotations

import os

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.trading.client import TradingClient

from glassbox.config import load_config, require_env


def _with_default_timeout(client):
    """Give every HTTP call this client makes a hard timeout.

    alpaca-py never sets one, and a requests call without a timeout blocks
    forever on a half-open socket. In the trader that wedges a thread until the
    supervisor notices the stale heartbeat; in the supervisor it would silently
    stop every guard from evaluating — the watchdog is the one process that can
    least afford to hang.
    """
    cfg = load_config()
    timeout = (
        cfg.execution.broker_connect_timeout_seconds,
        cfg.execution.broker_read_timeout_seconds,
    )
    session = client._session
    original = session.request

    def request_with_timeout(*args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original(*args, **kwargs)

    session.request = request_with_timeout
    return client


def trading_client() -> TradingClient:
    return _with_default_timeout(
        TradingClient(
            api_key=require_env("ALPACA_API_KEY_ID"),
            secret_key=require_env("ALPACA_API_SECRET_KEY"),
            paper=True,  # hard-pinned: this codebase never touches live endpoints
        )
    )


def supervisor_trading_client() -> TradingClient:
    """Client for the supervisor process.

    An Alpaca paper account issues one active key pair at a time, so separate
    credentials are only possible if the operator supplies them. What the
    supervisor guarantee actually requires is a separate *process* with its own
    client instance and connection pool, so a wedged trader can never block the
    kill switch — that holds either way. Distinct credentials, when available,
    are defence in depth on top of it.
    """
    return _with_default_timeout(
        TradingClient(
            api_key=os.environ.get("SUPERVISOR_ALPACA_API_KEY_ID")
            or require_env("ALPACA_API_KEY_ID"),
            secret_key=os.environ.get("SUPERVISOR_ALPACA_API_SECRET_KEY")
            or require_env("ALPACA_API_SECRET_KEY"),
            paper=True,
        )
    )


def stock_data_client() -> StockHistoricalDataClient:
    return _with_default_timeout(
        StockHistoricalDataClient(
            api_key=require_env("ALPACA_API_KEY_ID"),
            secret_key=require_env("ALPACA_API_SECRET_KEY"),
        )
    )


def option_data_client() -> OptionHistoricalDataClient:
    return _with_default_timeout(
        OptionHistoricalDataClient(
            api_key=require_env("ALPACA_API_KEY_ID"),
            secret_key=require_env("ALPACA_API_SECRET_KEY"),
        )
    )


def news_client() -> NewsClient:
    return _with_default_timeout(
        NewsClient(
            api_key=require_env("ALPACA_API_KEY_ID"),
            secret_key=require_env("ALPACA_API_SECRET_KEY"),
        )
    )
