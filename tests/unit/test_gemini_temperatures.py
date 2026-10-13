"""The temperature tagging call (Cayenne oven temperatures design, backfill)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.gemini import build_temperatures_prompt, tag_temperatures


def test_the_prompt_numbers_the_steps_and_names_the_system():
    prompt = build_temperatures_prompt(["Bake in a moderate oven.", "Serve."], "UK")
    assert "  1. Bake in a moderate oven." in prompt
    assert "  2. Serve." in prompt
    assert "SOURCE SYSTEM: UK" in prompt


def test_tag_temperatures_returns_the_mentions():
    reply = MagicMock(text=json.dumps({"mentions": [{"step": 1, "quote": "moderate oven", "context": "in a moderate oven", "value": "180C,oven"}]}))
    with patch("recipeparser.gemini._call_with_retry", return_value=reply):
        mentions = tag_temperatures(["Bake in a moderate oven."], None, MagicMock())
    assert [(m.step, m.quote, m.value) for m in mentions] == [(1, "moderate oven", "180C,oven")]


def test_an_empty_reply_raises():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text="")):
        with pytest.raises(RuntimeError):
            tag_temperatures(["Bake."], None, MagicMock())
