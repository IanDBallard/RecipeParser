"""Truncated-reply retry (spec 4.1). No real API calls — the client is a stub."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from recipeparser.exceptions import ExtractionParseError
from recipeparser.gemini import extract_recipes

VALID = '{"recipes": []}'
TRUNCATED = '{"recipes": [{"title": "Half a rec'


def _client(*replies: str) -> MagicMock:
    """A stub genai client whose generate_content returns each reply in turn."""
    client = MagicMock()
    responses = [
        SimpleNamespace(
            text=r,
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
        )
        for r in replies
    ]
    client.models.generate_content.side_effect = responses
    return client


def test_truncated_reply_is_retried_and_the_retry_wins(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client(TRUNCATED, VALID)

    result = extract_recipes("some chunk text", client)

    assert result.recipes == []
    assert client.models.generate_content.call_count == 2


def test_every_attempt_unparseable_raises_with_the_finish_reason(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client(TRUNCATED, TRUNCATED, TRUNCATED)

    with pytest.raises(ExtractionParseError) as excinfo:
        extract_recipes("some chunk text", client)

    assert "MAX_TOKENS" in str(excinfo.value)
    assert client.models.generate_content.call_count == 3


def test_an_empty_reply_is_retried_too(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client("", VALID)

    result = extract_recipes("some chunk text", client)

    assert result.recipes == []
    assert client.models.generate_content.call_count == 2
