"""Fix Roadmap F-132: the embedding call backs off like every other Gemini call.

Until this, one transient 503 or 429 on ``embed_content`` failed an ingest's
EMBED step, or a regeneration, outright. The errors are real, typed
google-genai errors and ``time.sleep`` is patched; nothing leaves the process.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors as genai_errors

from recipeparser.config import MAX_RETRIES
from recipeparser.gemini import get_embeddings

VECTOR = [0.1] * 1536
OK = SimpleNamespace(embeddings=[SimpleNamespace(values=VECTOR)], usage_metadata=None)


def _client(side_effect) -> MagicMock:
    client = MagicMock()
    client.models.embed_content.side_effect = side_effect
    return client


def _server_error(code: int = 503) -> Exception:
    return genai_errors.ServerError(code, {"message": f"{code} error", "status": "UNAVAILABLE"})


def _rate_limit() -> Exception:
    return genai_errors.ClientError(429, {"message": "429 quota", "status": "RESOURCE_EXHAUSTED"})


@pytest.mark.parametrize("error", [_server_error, _rate_limit])
def test_a_transient_error_is_retried_and_the_next_attempt_returns_the_vector(monkeypatch, error):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client([error(), OK])

    assert get_embeddings("pasta", client) == VECTOR
    assert client.models.embed_content.call_count == 2


def test_an_error_that_never_clears_raises_after_the_whole_ladder(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client([_server_error() for _ in range(MAX_RETRIES + 1)])

    with pytest.raises(genai_errors.ServerError):
        get_embeddings("pasta", client)
    assert client.models.embed_content.call_count == MAX_RETRIES + 1


def test_the_caller_can_shorten_the_ladder(monkeypatch):
    """The search endpoint answers a person who is waiting: one retry, not five."""
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client([_server_error(), _server_error(), OK])

    with pytest.raises(genai_errors.ServerError):
        get_embeddings("pasta", client, max_retries=1)
    assert client.models.embed_content.call_count == 2


def test_an_error_that_is_not_transient_raises_at_once(monkeypatch):
    slept = []
    monkeypatch.setattr("recipeparser.gemini.time.sleep", slept.append)
    client = _client([ValueError("bad request")])

    with pytest.raises(ValueError):
        get_embeddings("pasta", client)
    assert client.models.embed_content.call_count == 1 and slept == []


def test_a_retry_takes_a_rate_limiter_slot_and_the_first_attempt_does_not(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    limiter = MagicMock()
    client = _client([_server_error(), OK])

    get_embeddings("pasta", client, limiter=limiter)
    assert limiter.wait_then_record_start.call_count == 1
