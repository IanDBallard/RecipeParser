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
    assert "stovetop" in prompt and "Dutch oven" in prompt and "is not stovetop" in prompt
    assert "pie-savoury" in prompt and "quiche" in prompt


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


# --- Bake type definitions (Cayenne D29): decided by how the item is made, first fit in a fixed order ---


def _bake_rule():
    # The rule wraps at about 100 columns; compare words, not line breaks.
    return " ".join(build_pans_prompt(["Bake."], ["flour"], None).split())


def test_bake_types_are_decided_by_method_in_a_fixed_order():
    p = _bake_rule()
    assert "Decide by how the item is made, not by its name" in p
    order = ["1. pizza:", "2. cheesecake:", "3. a pie or tart,", "4. custard:", "5. bread-enriched / bread-lean:",
             "6. quick-bread:", "7. butter-cake:", "8. sponge:", "9. bar:"]
    positions = [p.index(label) for label in order]
    assert positions == sorted(positions)


def test_a_creamed_cake_is_butter_cake_and_sponge_means_a_foam():
    p = _bake_rule()
    assert "whisked or creamed" not in p
    assert "A creamed cake is butter-cake even when it is called a sponge" in p
    assert "eggs whisked to a foam" in p


def test_pie_savoury_needs_a_pastry_suet_or_mashed_potato_lid_and_excludes_bakes():
    p = _bake_rule()
    assert "or a topping" not in p
    assert "pastry, a suet crust or mashed potato" in p
    for excluded in ("lasagne", "moussaka", "enchiladas", "gratins", "anything sweet"):
        assert excluded in p


def test_yeast_cakes_are_enriched_bread_and_fruit_crumbles_are_pie_fruit():
    p = _bake_rule()
    assert "yeast cakes" in p
    assert "crumble, crisp or cobbler topping" in p


def test_null_names_the_common_non_bakes():
    p = _bake_rule()
    for item in ("roast meat", "casseroles", "cookies baked as separate pieces", "meringues", "on the hob"):
        assert item in p


def test_a_mashed_potato_lid_still_makes_a_pie():
    p = _bake_rule()
    assert "a lid of pastry, a suet crust or mashed potato covers it" in p
    assert "a mashed-potato lid is not a topping" in p


def test_pastry_alone_is_never_a_pie_and_batter_puddings_are_quick_bread():
    p = _bake_rule()
    assert "3. a pie or tart, never a recipe for pastry or dough alone:" in p
    assert "Yorkshire puddings and toad in the hole" in p
    assert "steamed sponge puddings" in p


def test_sweet_suet_puddings_and_fruit_pastries_are_pie_fruit_and_savoury_galettes_are_pie_savoury():
    p = _bake_rule()
    assert "sweet suet puddings and fruit-filled pastries (Sussex pond pudding, Eccles cakes, black bun)" in p
    assert "sweet fruit galettes" in p
    assert "savoury tarts and galettes" in p


def test_a_baking_dish_takes_the_family_of_its_shape_and_a_choice_takes_the_first_size():
    # The live library had 13 dimensioned dishes with no family ("an ovenproof dish, about 18 x 23cm"),
    # so no recipe baked in one could be fitted: the vocabulary named tins and pans, never dishes.
    p = _bake_rule()
    assert "A baking, ovenproof, gratin, casserole or pie dish takes the family of its shape" in p
    assert "rectangular traybake, square square, round round-cake, a pie dish tart" in p
    assert "the first size and shape it names" in p
    assert "A rectangle is traybake, never square: square only when its sides are equal" in p
