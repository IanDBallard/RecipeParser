"""The pans backfill's Gemini call (pan-aware scaling D22)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.gemini import _TINS_VOCAB_TEXT, build_pans_prompt, tag_pans
from recipeparser.models import PanAnswer

_REPLY = {
    "pans": [{"id": "pan_01", "raw": "two 20cm tins", "count": 2, "shape": "round", "family": "round-cake",
              "dims_mm": {"d": 200}}],
    "bake_type": "sponge", "bake_step": 1,
    "mentions": [{"step": 1, "quote": "two 20cm tins", "context": "Grease two 20cm tins", "pan_id": "pan_01"}],
    "core": [],
}


def test_the_prompt_numbers_steps_lists_ingredients_and_shares_the_vocabulary():
    prompt = build_pans_prompt(["Grease two 20cm tins.", "Bake."], ["200g flour", "2 eggs"], "UK")
    assert "  1. Grease two 20cm tins." in prompt and "  2. Bake." in prompt
    assert "200g flour" in prompt and "SOURCE SYSTEM: UK" in prompt
    assert _TINS_VOCAB_TEXT in prompt
    assert "tokenized_directions" not in prompt
    assert "in a tin token" not in prompt
    assert "1-based" in prompt and "may already sit inside" not in prompt
    assert "wrap the words naming each listed tin" not in prompt


def test_tag_pans_returns_a_pan_answer_at_temperature_zero():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text=json.dumps(_REPLY))) as call:
        answer = tag_pans(["Grease two 20cm tins.", "Bake."], ["flour"], None, MagicMock())
    assert isinstance(answer, PanAnswer) and answer.pans[0].id == "pan_01" and answer.bake_step == 0
    config = call.call_args.kwargs["config"]
    assert config["temperature"] == 0 and "response_json_schema" in config
    sent = json.dumps(config["response_json_schema"])
    assert "edited" not in sent and "tokenized_directions" not in sent


def _bake_step_from(gemini_says):
    reply = dict(_REPLY, bake_step=gemini_says)
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text=json.dumps(reply))):
        return tag_pans(["Grease.", "Bake."], [], None, MagicMock()).bake_step


def test_bake_step_is_asked_one_based_and_returned_zero_based():
    assert _bake_step_from(2) == 1
    assert _bake_step_from(1) == 0
    assert _bake_step_from(0) is None
    assert _bake_step_from(None) is None


def test_the_limiter_is_passed_through():
    limiter = object()
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text=json.dumps(_REPLY))) as call:
        tag_pans(["Bake."], [], None, MagicMock(), limiter=limiter)
    assert call.call_args.kwargs["limiter"] is limiter


def test_an_empty_reply_raises():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text="")):
        with pytest.raises(RuntimeError):
            tag_pans(["Bake."], [], None, MagicMock())
