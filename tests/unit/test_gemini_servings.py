"""The servings backfill's Gemini call."""
import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors as genai_errors

from recipeparser.exceptions import AIServiceTimeoutError, DailyQuotaExhaustedError, JobCancelledError
from recipeparser.gemini import estimate_servings


def test_estimate_servings_parses_the_reply():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text='{"servings": 10}')) as call:
        assert estimate_servings(MagicMock(), "Cake", ["flour"], ["Bake."]) == 10
    config = call.call_args.kwargs["config"]
    assert config["temperature"] == 0 and "servings" in json.dumps(config["response_json_schema"])


def test_estimate_servings_returns_none_on_garbage():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text="not json")):
        assert estimate_servings(MagicMock(), "Cake", [], []) is None
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text='{"servings": 0}')):
        assert estimate_servings(MagicMock(), "Cake", [], []) is None
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text='["not", "an", "object"]')):
        assert estimate_servings(MagicMock(), "Cake", [], []) is None
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text='{"other": 4}')):
        assert estimate_servings(MagicMock(), "Cake", [], []) is None


@pytest.mark.parametrize("exc", [
    genai_errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}),
    AIServiceTimeoutError("kept timing out"),
])
def test_estimate_servings_returns_none_on_an_api_failure_and_logs_it(exc, caplog):
    with patch("recipeparser.gemini._call_with_retry", side_effect=exc):
        with caplog.at_level(logging.WARNING, logger="recipeparser.gemini"):
            assert estimate_servings(MagicMock(), "Cake", [], []) is None
    assert any("Servings estimate for 'Cake' failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", [
    RuntimeError("a code bug"), AttributeError("x"), DailyQuotaExhaustedError(3600), JobCancelledError("stop"),
])
def test_estimate_servings_lets_unexpected_errors_propagate(exc):
    # Not "no estimate": a broken estimate must be visible.
    with patch("recipeparser.gemini._call_with_retry", side_effect=exc):
        with pytest.raises(type(exc)):
            estimate_servings(MagicMock(), "Cake", [], [])
