"""HTTP_TIMEOUT_SECS must actually reach the SDK (task 7c). No real API calls —
the client is a stub that records what it was called with.

google-genai's ``HttpOptions.timeout`` is expressed in MILLISECONDS (confirmed
against the installed google-genai==1.68.0: ``HttpOptions.timeout`` docstring
"Timeout for the request in milliseconds", and
``google.genai._api_client.get_timeout_in_seconds`` divides it by 1000.0 before
handing it to httpx). ``HTTP_TIMEOUT_SECS`` in recipeparser/config.py is 180
SECONDS, so the value that must reach the SDK is 180 * 1000 == 180000, not 180.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from recipeparser.config import HTTP_TIMEOUT_SECS
from recipeparser.gemini import _call_with_retry, _is_rate_limit_error


def _client(*replies, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.models.generate_content.side_effect = side_effect
    else:
        client.models.generate_content.side_effect = list(replies)
    return client


def test_timeout_reaches_generate_content_in_milliseconds_not_seconds():
    """The config passed to generate_content must carry a timeout of exactly
    HTTP_TIMEOUT_SECS * 1000 milliseconds — asserting mere presence would not
    catch a seconds/milliseconds mix-up, so assert the exact magnitude."""
    client = _client(SimpleNamespace(text="ok", candidates=[]))

    _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={"temperature": 0})

    assert client.models.generate_content.call_count == 1
    _, kwargs = client.models.generate_content.call_args
    sent_config = kwargs["config"]
    assert sent_config["http_options"]["timeout"] == 180_000
    # Pin the exact conversion so the test fails loudly if someone "fixes"
    # HTTP_TIMEOUT_SECS's unit instead of the call site.
    assert HTTP_TIMEOUT_SECS == 180
    assert sent_config["http_options"]["timeout"] == HTTP_TIMEOUT_SECS * 1000


def test_timeout_does_not_mutate_or_drop_the_callers_config():
    """The original config dict's other keys must survive untouched — the
    call path must not change model/contents/config semantics otherwise."""
    client = _client(SimpleNamespace(text="ok", candidates=[]))
    original_config = {
        "response_mime_type": "application/json",
        "temperature": 0.1,
    }

    _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config=original_config)

    _, kwargs = client.models.generate_content.call_args
    sent_config = kwargs["config"]
    assert sent_config["response_mime_type"] == "application/json"
    assert sent_config["temperature"] == 0.1
    # The caller's own dict must not have been mutated in place.
    assert "http_options" not in original_config


def test_a_hung_call_raises_rather_than_hanging_forever():
    """A timeout error from the transport must propagate out of
    _call_with_retry, not be swallowed or silently retried into a hang."""

    class _SimulatedHttpxTimeout(Exception):
        """Stands in for httpx.TimeoutException / httpx.ReadTimeout."""

    client = _client(side_effect=_SimulatedHttpxTimeout("timed out"))

    with pytest.raises(_SimulatedHttpxTimeout):
        _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    assert client.models.generate_content.call_count == 1


def test_timeout_error_is_not_misclassified_as_a_rate_limit_error():
    """Decision (task 7c, requirement 2): a transport timeout is NOT a
    rate-limit signal. ``_is_rate_limit_error`` only matches messages
    containing "429", "quota", or "resource_exhausted" — an httpx timeout
    message contains none of those, so it must return False and the retry
    loop in _call_with_retry must not apply the 5x exponential back-off to
    it; it must raise on the very first attempt instead."""

    class _SimulatedHttpxTimeout(Exception):
        pass

    timeout_exc = _SimulatedHttpxTimeout("timed out")
    assert _is_rate_limit_error(timeout_exc) is False

    client = _client(side_effect=timeout_exc)

    with pytest.raises(_SimulatedHttpxTimeout):
        _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    # Exactly one attempt: no back-off sleep, no retries burned on a timeout.
    assert client.models.generate_content.call_count == 1


def test_rate_limit_retry_still_works_with_the_timeout_wired_in(monkeypatch):
    """Regression: adding the timeout to the config must not disturb the
    existing 429 retry/back-off behaviour."""
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)

    rate_limit_exc = Exception("429 RESOURCE_EXHAUSTED: quota exceeded")
    ok_response = SimpleNamespace(text="ok", candidates=[])
    client = _client(side_effect=[rate_limit_exc, ok_response])

    result = _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    assert result is ok_response
    assert client.models.generate_content.call_count == 2
    # The retried call must still carry the timeout.
    _, kwargs = client.models.generate_content.call_args
    assert kwargs["config"]["http_options"]["timeout"] == 180_000
