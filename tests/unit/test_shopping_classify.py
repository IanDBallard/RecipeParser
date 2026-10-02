"""The classify module: prompt layout, structural checks, the one check-retry.

The model itself is proved by the recorded goldens (tests/goldens/
test_classify_golden.py); here every reply is scripted.
"""
from __future__ import annotations

import json
from typing import List, Optional

import pytest

from recipeparser.shopping import (
    AISLES,
    ClassifyCheckError,
    ClassifyIngredient,
    build_classify_prompt,
    classify_ingredients,
)

ING = [
    ClassifyIngredient(key="r1:i1", text="2 cups plain flour", name="plain flour", amount=2.0, unit="cup"),
    ClassifyIngredient(key="r1:i2", text="salt to taste", name="salt", amount=None, unit=None),
]


def item(key: str, food: str = "flour", aisle: str = "dry_goods", pantry: bool = True,
         count: Optional[float] = None, count_unit: Optional[str] = None) -> dict:
    return {"key": key, "food": food, "aisle": aisle, "pantry": pantry,
            "count": count, "count_unit": count_unit}


def reply(items: List[dict]) -> str:
    return json.dumps({"items": items})


class ScriptedClient:
    """client.models.generate_content answering from a list of texts, in order."""

    def __init__(self, texts: List[str]) -> None:
        self._texts = list(texts)
        self.calls: List[dict] = []

        outer = self

        class _Models:
            def generate_content(self, *, model, contents, config):
                outer.calls.append({"model": model, "contents": contents, "config": config})

                class _R:
                    text = outer._texts.pop(0)

                return _R()

        self.models = _Models()


GOOD = reply([item("r1:i1"), item("r1:i2", food="salt", aisle="spices", count=None)])


class TestPrompt:
    def test_the_marker_and_sections_are_present(self):
        prompt = build_classify_prompt(ING, ["flour", "spring onion"])
        head = prompt[:200].lower()
        assert "grocery shopping classifier" in head
        assert "KNOWN FOODS:" in prompt
        assert "INGREDIENTS:" in prompt
        # The body (what the golden client keys on) starts at KNOWN FOODS and
        # carries both lists, so a recording is keyed to its inputs.
        body = prompt[prompt.index("KNOWN FOODS:"):]
        assert "spring onion" in body and "plain flour" in body

    def test_known_foods_are_stripped_and_deduped(self):
        prompt = build_classify_prompt(ING, [" flour ", "flour", "", "  "])
        body = prompt[prompt.index("KNOWN FOODS:"):prompt.index("INGREDIENTS:")]
        assert body.count('"flour"') == 1
        assert '""' not in body

    def test_the_twelve_aisles_in_order(self):
        assert AISLES == ("produce", "meat_fish", "dairy_eggs", "bakery", "dry_goods",
                          "spices", "condiments", "tins_jars", "frozen", "drinks",
                          "household", "other")


class TestChecks:
    def test_a_good_reply_comes_back_as_items(self):
        client = ScriptedClient([GOOD])
        items = classify_ingredients(client, ING, [])
        assert [i.key for i in items] == ["r1:i1", "r1:i2"]
        assert len(client.calls) == 1

    def test_the_call_uses_the_model_schema_and_temperature(self):
        client = ScriptedClient([GOOD])
        classify_ingredients(client, ING, [])
        call = client.calls[0]
        from recipeparser.config import GEMINI_MODEL
        assert call["model"] == GEMINI_MODEL
        assert call["config"]["temperature"] == 0.1
        assert call["config"]["response_mime_type"] == "application/json"
        assert "response_json_schema" in call["config"]
        assert call["config"]["http_options"] == {"timeout": 60_000}

    @pytest.mark.parametrize("bad, reason_word", [
        (reply([item("r1:i1")]), "key"),                                     # missing key
        (reply([item("r1:i1"), item("r1:i2"), item("r1:i3")]), "key"),       # extra key
        (reply([item("r1:i1"), item("r1:i1")]), "key"),                      # repeated key
        (reply([item("r1:i1"), item("r1:i2", count=-1, count_unit="x")]), "count"),
        (reply([item("r1:i1"), item("r1:i2", count=float("nan"), count_unit="x")]), "count"),
        (reply([item("r1:i1"), item("r1:i2", count=2.0, count_unit=None)]), "count_unit"),
        (reply([item("r1:i1"), item("r1:i2", count=None, count_unit="tin")]), "count_unit"),
        (reply([item("r1:i1"), item("r1:i2", food="  ")]), "food"),
        # Not "aisle": ClassifiedItem.aisle is the Literal[Aisle] the schema
        # constrains the model to (AISLES's own comment), so Pydantic rejects
        # an out-of-enum value at model_validate() — before _check() ever
        # runs its own `item.aisle not in AISLES` line. This is the "could
        # not be read" branch, same as unparseable JSON.
        (reply([item("r1:i1"), item("r1:i2", aisle="pet_food")]), "read"),
        ("not json at all", "read"),
    ])
    def test_a_failed_check_retries_once_then_raises_the_reason(self, bad, reason_word):
        client = ScriptedClient([bad, bad])
        with pytest.raises(ClassifyCheckError) as err:
            classify_ingredients(client, ING, [])
        assert len(client.calls) == 2
        assert reason_word in str(err.value).lower()

    def test_a_failed_check_then_a_good_reply_succeeds(self):
        client = ScriptedClient([reply([item("r1:i1")]), GOOD])
        items = classify_ingredients(client, ING, [])
        assert len(items) == 2
        assert len(client.calls) == 2

    def test_unicode_survives(self):
        ing = [ClassifyIngredient(key="k", text="200ml crème fraîche", name="crème fraîche")]
        client = ScriptedClient([reply([item("k", food="crème fraîche", aisle="dairy_eggs",
                                             pantry=False)])])
        assert "crème fraîche" in build_classify_prompt(ing, [])
        assert classify_ingredients(client, ing, [])[0].food == "crème fraîche"


class TestFinalizeTimeout:
    """Task 1's one change to gemini.py: the HTTP timeout is a ceiling, not an override."""

    def test_tighter_kept_longer_capped_absent_defaulted(self):
        from recipeparser.gemini import _HTTP_TIMEOUT_MS, _finalize_config

        assert _finalize_config({"http_options": {"timeout": 60_000}})["http_options"]["timeout"] == 60_000
        assert _finalize_config({})["http_options"]["timeout"] == _HTTP_TIMEOUT_MS
        assert _finalize_config({"http_options": {"timeout": 999_999_999}})["http_options"]["timeout"] == _HTTP_TIMEOUT_MS

    def test_a_falsy_caller_timeout_becomes_the_default_not_unbounded(self):
        """0 must not reach min() as an unbounded request, and None must not
        reach it as a TypeError against _HTTP_TIMEOUT_MS (the ceiling hole)."""
        from recipeparser.gemini import _HTTP_TIMEOUT_MS, _finalize_config

        assert _finalize_config({"http_options": {"timeout": 0}})["http_options"]["timeout"] == _HTTP_TIMEOUT_MS
        assert _finalize_config({"http_options": {"timeout": None}})["http_options"]["timeout"] == _HTTP_TIMEOUT_MS
