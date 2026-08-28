"""The broker clients must never make an unbounded HTTP call.

alpaca-py sets no timeout, and a requests call without one blocks forever on a
half-open socket. In the trader that wedges a thread; in the supervisor it
would silently stop every guard from evaluating.
"""

from types import SimpleNamespace

from glassbox.data.alpaca_client import _with_default_timeout


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(kwargs)
        return "ok"


def _fake_client():
    return SimpleNamespace(_session=FakeSession())


def test_timeout_is_injected_into_every_request():
    client = _with_default_timeout(_fake_client())
    client._session.request("GET", "https://paper-api.alpaca.markets/v2/account")
    (call,) = client._session.calls
    connect, read = call["timeout"]
    assert connect > 0 and read > 0


def test_explicit_timeout_is_respected():
    client = _with_default_timeout(_fake_client())
    client._session.request("GET", "https://x", timeout=(1, 2))
    assert client._session.calls[0]["timeout"] == (1, 2)


def test_none_timeout_is_treated_as_unset():
    """requests treats timeout=None as 'wait forever' — exactly the behavior
    this wrapper exists to eliminate."""
    client = _with_default_timeout(_fake_client())
    client._session.request("GET", "https://x", timeout=None)
    assert client._session.calls[0]["timeout"] is not None


def test_real_clients_get_wrapped(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    from glassbox.data.alpaca_client import (
        news_client,
        option_data_client,
        stock_data_client,
        supervisor_trading_client,
        trading_client,
    )

    for factory in (trading_client, supervisor_trading_client, stock_data_client,
                    option_data_client, news_client):
        client = factory()  # construction makes no network calls
        assert client._session.request.__name__ == "request_with_timeout", factory.__name__
