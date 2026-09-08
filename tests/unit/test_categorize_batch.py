"""Bulk recategorise helpers (spec 6.2, 6.4)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.config import GEMINI_MODEL
from recipeparser.core.stages.categorize import chunked, filter_batch_result


def test_chunked():
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunked([], 3) == []


def test_filter_drops_unoffered_and_dedups():
    result = {"r1": ["Italian", "Vegan", "Italian"], "r2": ["Nope"], "r3": []}
    assert filter_batch_result(result, {"Italian", "Vegan"}) == {"r1": ["Italian", "Vegan"]}


def test_categorize_batch_prompt_and_parse():
    from recipeparser.gemini import categorize_batch
    reply = MagicMock(text=json.dumps({"results": [
        {"recipe_id": "r1", "tags": ["Italian"]},
        {"recipe_id": "r2", "tags": []},
    ]}))
    with patch("recipeparser.gemini._call_with_retry", return_value=reply) as call:
        out = categorize_batch(
            [{"id": "r1", "title": "Lasagne", "ingredient_lines": ["pasta"], "direction_steps": ["Bake."]},
             {"id": "r2", "title": "Toast", "ingredient_lines": ["bread"], "direction_steps": ["Toast."]}],
            {"Cuisine": ["Italian", "Thai"]},
            client=MagicMock(),
        )
    assert out == {"r1": ["Italian"], "r2": []}
    prompt = call.call_args.kwargs["contents"]
    assert "Italian" in prompt and "Thai" in prompt and "Lasagne" in prompt and "r2" in prompt
    assert call.call_args.kwargs["model"] == GEMINI_MODEL
    assert call.call_args.kwargs["what"] == "categorize_batch"


def test_categorize_batch_empty_reply_raises():
    # An empty reply is a failed batch, not "nothing matched". It must reach
    # RecatWorker's per-batch handler so the batch is counted as failed.
    from recipeparser.gemini import categorize_batch
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text="")):
        with pytest.raises(ValueError, match="empty response"):
            categorize_batch([{"id": "r1", "title": "x", "ingredient_lines": [], "direction_steps": []}],
                             {"Cuisine": ["Italian"]}, client=MagicMock())


def test_categorize_batch_call_failure_propagates():
    # Same for the call itself: swallowing it here would leave RecatWorker's
    # 10% failure threshold as dead code.
    from recipeparser.gemini import categorize_batch
    with patch("recipeparser.gemini._call_with_retry", side_effect=RuntimeError("gemini down")):
        with pytest.raises(RuntimeError, match="gemini down"):
            categorize_batch([{"id": "r1", "title": "x", "ingredient_lines": [], "direction_steps": []}],
                             {"Cuisine": ["Italian"]}, client=MagicMock())


def test_categorize_batch_no_matches_is_not_a_failure():
    # A well-formed reply where nothing matched is a successful, empty result.
    from recipeparser.gemini import categorize_batch
    reply = MagicMock(text=json.dumps({"results": [{"recipe_id": "r1", "tags": []}]}))
    with patch("recipeparser.gemini._call_with_retry", return_value=reply):
        assert categorize_batch([{"id": "r1", "title": "x", "ingredient_lines": [], "direction_steps": []}],
                                {"Cuisine": ["Italian"]}, client=MagicMock()) == {"r1": []}
