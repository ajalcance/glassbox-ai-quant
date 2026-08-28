"""LLM client tests using a stub — no network, no key required.

The point of these is the defensive behaviour: Fireworks is documented to
silently downgrade strict json_schema enforcement, so we must never rely on the
provider validating anything for us.
"""

import pytest

from glassbox.llm import LlmClient, LlmSchemaError, LlmUnavailableError
from glassbox.signal.analyst import AnalystView

GOOD = (
    '{"event_type":"earnings","direction":"up","confidence":0.8,'
    '"expected_move_pct":4.5,"horizon_hours":24,"materiality":0.9,'
    '"rationale":"Beat and raised."}'
)


class StubCompletion:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class StubClient:
    """Returns each queued response in turn; records what it was asked."""

    def __init__(self, responses, raises=None):
        self.responses = list(responses)
        self.raises = raises
        self.calls = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return StubCompletion(self.responses.pop(0))


def client_with(responses, raises=None):
    c = LlmClient(base_url="http://stub", api_key="k")
    c._client = StubClient(responses, raises)
    return c


def test_valid_response_parsed():
    c = client_with([GOOD])
    view = c.extract("m", "sys", "usr", AnalystView)
    assert view.direction == "up" and view.expected_move_pct == 4.5
    assert view.is_directional


def test_schema_is_sent_to_provider():
    c = client_with([GOOD])
    c.extract("m", "sys", "usr", AnalystView)
    rf = c._client.calls[0]["response_format"]
    assert "schema" in rf, "schema must be sent even though we also validate locally"


def test_malformed_json_retried_once_then_succeeds():
    c = client_with(["not json at all", GOOD])
    view = c.extract("m", "sys", "usr", AnalystView)
    assert view.event_type == "earnings"
    assert len(c._client.calls) == 2


def test_retry_shows_the_model_its_own_failure():
    c = client_with(["garbage", GOOD])
    c.extract("m", "sys", "usr", AnalystView)
    retry_messages = c._client.calls[1]["messages"]
    assert any("did not match the required schema" in m["content"] for m in retry_messages)


def test_persistent_garbage_raises_rather_than_guessing():
    c = client_with(["nope", "still nope"])
    with pytest.raises(LlmSchemaError):
        c.extract("m", "sys", "usr", AnalystView)


def test_schema_violating_but_valid_json_is_rejected():
    """The provider may downgrade enforcement — local validation must catch it."""
    bad = (
        '{"event_type":"earnings","direction":"up","confidence":5.0,'  # >1.0
        '"expected_move_pct":4.5,"horizon_hours":24,"materiality":0.9,'
        '"rationale":"x"}'
    )
    c = client_with([bad, bad])
    with pytest.raises(LlmSchemaError):
        c.extract("m", "sys", "usr", AnalystView)


def test_negative_expected_move_rejected():
    bad = (
        '{"event_type":"legal","direction":"down","confidence":0.7,'
        '"expected_move_pct":-3.0,"horizon_hours":24,"materiality":0.5,'
        '"rationale":"x"}'
    )
    c = client_with([bad, bad])
    with pytest.raises(LlmSchemaError):
        c.extract("m", "sys", "usr", AnalystView)


def test_provider_outage_raises_unavailable():
    c = client_with([], raises=ConnectionError("fireworks 503"))
    with pytest.raises(LlmUnavailableError):
        c.extract("m", "sys", "usr", AnalystView)


def test_temperature_is_zero_by_default():
    """Trading decisions should not vary run to run on the same input."""
    c = client_with([GOOD])
    c.extract("m", "sys", "usr", AnalystView)
    assert c._client.calls[0]["temperature"] == 0.0
