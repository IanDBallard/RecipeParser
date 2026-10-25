"""The picture food check's model call (Cayenne Fix Roadmap F-209)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from recipeparser import usage
from recipeparser.config import FOOD_CHECK_TIMEOUT_SECS, GEMINI_FOOD_CHECK_MODEL
from recipeparser.gemini import FOOD_CHECK_PROMPT, FOOD_CHECK_STEP, ask_is_food


def _reply(kind: str) -> MagicMock:
    return MagicMock(text=json.dumps({"kind": kind}))


def test_the_verdict_is_parsed_into_its_typed_kind():
    with patch("recipeparser.gemini._call_with_retry", return_value=_reply("vehicle")):
        verdict = ask_is_food(MagicMock(), b"jpeg", "image/jpeg")
    assert verdict.kind == "vehicle"


def test_a_reply_outside_the_kinds_raises_so_the_picture_is_kept_unchecked():
    with patch("recipeparser.gemini._call_with_retry", return_value=_reply("a lovely duck")):
        with pytest.raises(ValueError):
            ask_is_food(MagicMock(), b"jpeg", "image/jpeg")


def test_an_empty_reply_raises():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text="")):
        with pytest.raises(RuntimeError):
            ask_is_food(MagicMock(), b"jpeg", "image/jpeg")


def test_one_short_bounded_call_with_the_image_and_the_prompt():
    with patch("recipeparser.gemini._call_with_retry", return_value=_reply("dish")) as call:
        ask_is_food(MagicMock(), b"jpeg-bytes", "image/jpeg")
    kwargs = call.call_args.kwargs
    assert kwargs["model"] == GEMINI_FOOD_CHECK_MODEL
    assert kwargs["what"] == FOOD_CHECK_STEP
    assert kwargs["max_retries"] == 1
    part, prompt = kwargs["contents"]
    assert part.inline_data.data == b"jpeg-bytes" and part.inline_data.mime_type == "image/jpeg"
    assert prompt == FOOD_CHECK_PROMPT
    config = kwargs["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_json_schema"]["properties"]["kind"]["enum"][:3] == ["dish", "ingredients", "cooking"]
    assert config["media_resolution"] == "MEDIA_RESOLUTION_LOW"
    assert config["http_options"]["timeout"] == FOOD_CHECK_TIMEOUT_SECS * 1000


def test_the_prompt_names_every_kind_the_schema_allows():
    from typing import get_args

    from recipeparser.core.picture_check import FoodKind

    for kind in get_args(FoodKind):
        assert kind in FOOD_CHECK_PROMPT


def test_the_call_is_counted_in_ai_usage_under_its_own_step():
    """Recorded like every other Gemini call: through _call_with_retry's usage record, in the
    caller's attribution (an import's)."""
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(
        text=json.dumps({"kind": "logo"}),
        usage_metadata=MagicMock(prompt_token_count=430, candidates_token_count=6, thoughts_token_count=None),
    )
    ctx = usage.UsageContext(user_id="u1", action="import", job_id="j1")
    token = usage.ai_context.set(ctx)
    try:
        ask_is_food(client, b"jpeg", "image/jpeg")
    finally:
        usage.ai_context.reset(token)
    [row] = ctx.records
    assert (row["action"], row["step"], row["job_id"], row["prompt_tokens"]) == ("import", FOOD_CHECK_STEP, "j1", 430)
