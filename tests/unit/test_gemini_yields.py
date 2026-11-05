"""The yields-only Gemini call (Cayenne yield kinds 11.3)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.core.yields import PASTRY_TYPES, SIDES, THICKNESSES, TOPS, YIELD_KINDS
from recipeparser.gemini import _YIELD_RULES_TEXT, build_yields_prompt, tag_yield
from recipeparser.models import RecipePan, StructuredIngredient, YieldAnswer

_REPLY = {
    "kind": "servings",
    "container": {"pastry": "shortcrust", "base": True, "sides": "full", "top": "full", "thickness": "standard",
                  "component_lines": [2], "topping_lines": []},
    "area": None,
    "pieces": None,
}

_ING = [
    StructuredIngredient(id="ing_01", kind="heading", name="Pastry", fallback_string="Pastry:"),
    StructuredIngredient(id="ing_02", kind="ingredient", name="flour", fallback_string="300g flour"),
]
_PAN = RecipePan(id="pan_01", raw="a 9-inch pie plate", shape="round", family="pie")


def test_the_prompt_numbers_every_ingredient_from_one_headings_included():
    prompt = build_yields_prompt(["Line the plate.", "Bake."], _ING, [_PAN], "pie-fruit")
    assert "  1. (heading) Pastry:" in prompt and "  2. 300g flour" in prompt
    assert "  1. Line the plate." in prompt and "  2. Bake." in prompt


def test_the_prompt_names_the_tins_and_the_bake_type():
    prompt = build_yields_prompt(["Bake."], _ING, [_PAN], "pie-fruit")
    assert "a 9-inch pie plate" in prompt and "BAKE TYPE: pie-fruit" in prompt
    none = build_yields_prompt(["Bake."], _ING, [], None)
    assert "BAKE TYPE: none" in none and "TINS: none" in none


def test_the_prompt_carries_the_rules_with_every_vocabulary_value():
    prompt = build_yields_prompt(["Bake."], _ING, [], None)
    assert _YIELD_RULES_TEXT in prompt
    for value in (*YIELD_KINDS, *PASTRY_TYPES, *SIDES, *TOPS, *THICKNESSES):
        assert value in prompt
    assert "{kinds}" not in prompt and "{pastry}" not in prompt


def test_the_kind_test_comes_before_the_container():
    rules = " ".join(_YIELD_RULES_TEXT.split())
    assert rules.index("1. component:") < rules.index("2. pieces:") < rules.index("3. servings:")
    assert rules.index("3. servings:") < rules.index('"container":')


def test_tag_yield_returns_a_yield_answer_at_temperature_zero():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text=json.dumps(_REPLY))) as call:
        answer = tag_yield(["Line the plate."], _ING, [_PAN], "pie-fruit", MagicMock())
    assert isinstance(answer, YieldAnswer) and answer.kind == "servings"
    assert answer.container.component_lines == [2]
    config = call.call_args.kwargs["config"]
    assert config["temperature"] == 0 and "response_json_schema" in config
    sent = json.dumps(config["response_json_schema"])
    assert "edited" not in sent and "component_lines" in sent
    assert "description" in sent


def test_the_limiter_is_passed_through():
    limiter = object()
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text=json.dumps(_REPLY))) as call:
        tag_yield(["Bake."], _ING, [], None, MagicMock(), limiter=limiter)
    assert call.call_args.kwargs["limiter"] is limiter


def test_an_empty_reply_raises():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text="")):
        with pytest.raises(RuntimeError):
            tag_yield(["Bake."], _ING, [], None, MagicMock())


@pytest.mark.parametrize("bad", [
    {"kind": 3}, {"container": "shortcrust"}, {"area": [1]}, {"pieces": 4},
    {"kind": "component", "area": {"count": "two", "diameter_mm": "wide"}},
    {"container": {"base": "yes", "component_lines": "ing_01", "pastry": 4}},
])
def test_a_shape_slip_never_makes_tag_yield_raise(bad):
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text=json.dumps(bad))):
        answer = tag_yield(["Bake."], _ING, [], None, MagicMock())
    assert isinstance(answer, YieldAnswer)


def test_a_slip_costs_only_its_field():
    reply = {"kind": "component", "container": "shortcrust", "area": {"count": 2, "diameter_mm": "wide"}}
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text=json.dumps(reply))):
        answer = tag_yield(["Bake."], _ING, [], None, MagicMock())
    assert answer.kind == "component" and answer.container is None
    assert answer.area.count == 2 and answer.area.diameter_mm is None


def test_the_yields_prompt_says_the_answer_is_the_yield_object_itself():
    prompt = build_yields_prompt(["Bake."], _ING, [], None)
    assert 'Answer with the "yield_spec" object itself' in prompt


def test_the_prompt_carries_the_yield_line_when_given_and_omits_it_otherwise():
    with_yield = build_yields_prompt(["Bake."], _ING, [], None, yield_text="16 Enough for double crust")
    assert "YIELD: 16 Enough for double crust" in with_yield
    assert "YIELD:" not in build_yields_prompt(["Bake."], _ING, [], None)
    assert "YIELD:" not in build_yields_prompt(["Bake."], _ING, [], None, yield_text=None)


def test_tag_yield_passes_the_yield_text_into_the_prompt():
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text=json.dumps(_REPLY))) as call:
        tag_yield(["Bake."], _ING, [], None, MagicMock(), yield_text="Enough for double crust")
    assert "YIELD: Enough for double crust" in call.call_args.kwargs["contents"]


def test_yield_text_from_a_stored_rows_servings_columns():
    from recipeparser.core.yields import yield_text

    assert yield_text(16, 16, "Enough for double crust for a standard pie plate") == \
        "16 Enough for double crust for a standard pie plate"
    assert yield_text(16, None, "Enough for double crust") == "16 Enough for double crust"
    assert yield_text(2, 4, None) == "2 to 4"
    assert yield_text(4, 4, "") == "4"
    assert yield_text(None, None, "Makes one loaf") == "Makes one loaf"
    assert yield_text(None, None, None) is None and yield_text(None, None, "  ") is None
