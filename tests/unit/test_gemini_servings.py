"""The servings backfill's Gemini call."""
import json
from unittest.mock import MagicMock, patch

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
    with patch("recipeparser.gemini._call_with_retry", side_effect=RuntimeError("quota")):
        assert estimate_servings(MagicMock(), "Cake", [], []) is None
