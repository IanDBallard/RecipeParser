"""Transient server-error retry (task 7d).

A `504 DEADLINE_EXCEEDED` recording run died partway through a fixture because
``_is_rate_limit_error`` only matches "429"/"quota"/"resource_exhausted" — a
transient 5xx/UNAVAILABLE/DEADLINE_EXCEEDED/INTERNAL error raised on attempt 1
instead of using the existing back-off ladder. No real API calls — the client
is a stub, and ``time.sleep`` is patched so the back-off ladder never actually
sleeps.

The trap this file exists to guard against — and empirically hit while
writing this: a naive `"500" in str(exc)` / `"unavailable" in str(exc)`
classifier has TWO real false positives in this very codebase, not one:

1. ``tests/goldens/golden_client.py`` raises ``MissingRecordingError`` whose
   message embeds a file path with a hex sha8 directory (e.g.
   ``.../gemini/f.epub/8fb1500a/extract-00.json``), which contains the
   substring "500". See
   ``test_missing_recording_error_is_never_retried_even_with_500_in_its_path``.
2. ``tests/test_gemini.py`` has two PRE-EXISTING, unrelated tests
   (``test_api_exception_propagates``, one under ``TestExtractRecipes`` and
   one under ``TestExtractRecipeFromText``) that raise a plain
   ``Exception("503 Service Unavailable")`` with ``time.sleep`` unpatched,
   expecting it to propagate on attempt 1. A first pass at this task added a
   text-based fallback for untyped exceptions and it reclassified that exact
   message as retryable, turning the ~4s suite into a ~130s one by actually
   sleeping through the real 5x back-off ladder twice. See
   ``test_generic_untyped_exception_with_server_like_text_is_not_retried``,
   which pins the same scenario here with ``time.sleep`` patched so a
   regression fails fast instead of just slowly.

Because of (2), this file's detector (``_is_transient_server_error`` in
``recipeparser/gemini.py``) does NOT fall back to message inspection at all
for the new transient-server classification: it is exclusively typed,
matching ``google.genai.errors`` types and their numeric ``.code`` / string
``.status`` attributes. Every transient-error case below is therefore
constructed as a real, typed SDK error — which is also what the real client
actually raises on an HTTP-level failure.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors as genai_errors

from recipeparser.config import MAX_RETRIES
from recipeparser.gemini import _call_with_retry, _is_rate_limit_error
from tests.goldens.golden_client import MissingRecordingError

OK_RESPONSE = SimpleNamespace(text="ok", candidates=[])


def _client(side_effect) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.side_effect = side_effect
    return client


def _server_error(code: int, status: str | None = None) -> Exception:
    """A real, typed google-genai ServerError, as raised by APIError.raise_error
    for any 5xx status code."""
    return genai_errors.ServerError(code, {"message": f"{code} error", "status": status})


def _untyped_status_error(status: str) -> Exception:
    """A real, typed google-genai APIError carrying a gRPC-style status name
    but a non-5xx numeric code — exercises the ``.status`` fallback branch of
    ``_is_transient_server_error`` independently of the ``.code`` branch."""
    return genai_errors.APIError(1, {"message": status, "status": status})


# Six transient-server-error shapes named in the task brief. All are real,
# typed google.genai.errors instances (never a plain Exception) — see the
# module docstring for why a message-based fallback is deliberately not used.
TRANSIENT_ERRORS = {
    "500": lambda: _server_error(500, "INTERNAL"),
    "502": lambda: _server_error(502, None),
    "503": lambda: _server_error(503, "UNAVAILABLE"),
    "504": lambda: _server_error(504, "DEADLINE_EXCEEDED"),
    "UNAVAILABLE (status-only)": lambda: _untyped_status_error("UNAVAILABLE"),
    "DEADLINE_EXCEEDED (status-only)": lambda: _untyped_status_error("DEADLINE_EXCEEDED"),
}


@pytest.mark.parametrize("label", TRANSIENT_ERRORS)
def test_transient_server_error_is_retried_and_a_later_attempt_succeeds(monkeypatch, label):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    exc = TRANSIENT_ERRORS[label]()
    client = _client([exc, OK_RESPONSE])

    result = _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    assert result is OK_RESPONSE
    assert client.models.generate_content.call_count == 2


@pytest.mark.parametrize("label", TRANSIENT_ERRORS)
def test_transient_server_error_that_never_recovers_raises_after_all_attempts(monkeypatch, label):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    make_exc = TRANSIENT_ERRORS[label]
    # A fresh exception instance per call — real errors aren't reused across attempts.
    client = _client([make_exc() for _ in range(MAX_RETRIES + 1)])

    with pytest.raises(Exception):
        _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    # MAX_RETRIES retries plus the original attempt.
    assert client.models.generate_content.call_count == MAX_RETRIES + 1


def test_client_error_raises_on_attempt_1_with_no_retry(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    exc = genai_errors.ClientError(400, {"message": "bad request", "status": "INVALID_ARGUMENT"})
    client = _client([exc])

    with pytest.raises(genai_errors.ClientError):
        _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    assert client.models.generate_content.call_count == 1


def test_missing_recording_error_is_never_retried_even_with_500_in_its_path(monkeypatch):
    """Pins the trap called out in the task brief: a MissingRecordingError
    whose message embeds a hex sha8 path segment containing "500" must never
    be classified as a retryable transient server error."""
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    exc = MissingRecordingError(
        "No recorded Gemini reply at "
        "tests/goldens/gemini/f.epub/8fb1500a/extract-00.json"
    )
    assert "500" in str(exc)  # sanity: the trap condition really is present
    client = _client([exc])

    with pytest.raises(MissingRecordingError):
        _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    assert client.models.generate_content.call_count == 1


def test_generic_untyped_exception_with_server_like_text_is_not_retried(monkeypatch):
    """Pins the second false-positive found while writing this file:
    tests/test_gemini.py has two pre-existing, unrelated tests that raise a
    plain ``Exception("503 Service Unavailable")`` with ``time.sleep``
    unpatched, expecting immediate propagation. Detection here is exclusively
    typed (google.genai.errors), so a plain Exception — regardless of
    wording — is never classified as a transient server error."""
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    exc = Exception("503 Service Unavailable")
    client = _client([exc])

    with pytest.raises(Exception, match="503 Service Unavailable"):
        _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    assert client.models.generate_content.call_count == 1


def test_rate_limit_retry_is_unchanged(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    rate_limit_exc = Exception("429 RESOURCE_EXHAUSTED: quota exceeded")
    client = _client([rate_limit_exc, OK_RESPONSE])

    result = _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    assert result is OK_RESPONSE
    assert client.models.generate_content.call_count == 2
    assert _is_rate_limit_error(rate_limit_exc) is True


def test_client_side_timeout_still_raises_on_attempt_1(monkeypatch):
    """Task 7c's decision, unchanged: an httpx-style transport timeout already
    waited the full HTTP_TIMEOUT_SECS, so it must not be put on the retry
    ladder — a server-returned 504 is a different thing and IS retried
    (see the parametrized transient-error tests above)."""
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)

    class _SimulatedHttpxTimeout(Exception):
        pass

    exc = _SimulatedHttpxTimeout("timed out")
    client = _client([exc])

    with pytest.raises(_SimulatedHttpxTimeout):
        _call_with_retry(client, model="gemini-2.5-flash", contents="hi", config={})

    assert client.models.generate_content.call_count == 1
