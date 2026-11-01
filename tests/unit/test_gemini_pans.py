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


def test_a_crust_alone_is_pie_shell_other_pastry_is_null_and_batter_puddings_are_quick_bread():
    # Cayenne pie tins D57: a pie or tart crust alone is pie-shell; puff for palmiers or choux stays null.
    # It is the last step, and step 3's crust-only-bake null stops there: inside step 3 the live probe
    # read banana cream, chiffon and banoffee pie as pie-shell.
    p = _bake_rule()
    assert "3. a pie or tart, never a recipe for a crust or pastry alone (step 10):" in p
    assert "is null and stops here, never pie-shell (banana cream, chiffon and banoffee pie" in p
    assert (
        "10. pie-shell: a recipe for a pie or tart crust alone (pastry or a crumb crust, baked empty or not baked);"
        " other pastry or dough alone is null. The recipe's whole result is an empty crust" in p
    )
    assert p.index("10. pie-shell:") > p.index("9. bar:")
    assert "Yorkshire puddings and toad in the hole" in p
    assert "steamed sponge puddings" in p


def test_sweet_suet_puddings_and_fruit_pastries_are_pie_fruit_and_savoury_galettes_are_pie_savoury():
    p = _bake_rule()
    assert "sweet suet puddings and fruit-filled pastries (Sussex pond pudding, Eccles cakes, black bun)" in p
    assert "sweet fruit galettes" in p
    assert "savoury tarts and galettes" in p


def test_a_pie_whose_filling_never_bakes_has_no_bake_type():
    # Cayenne F-487: Banana Cream, Lime Chiffon and Mango Pie were pie-custard with the crust's bake as
    # their bake step, so the app showed a custard's set cue under a crumb crust.
    p = _bake_rule()
    assert "Only when the filling bakes in it" in p
    assert "only a meringue is browned on top, is null" in p
    assert "banana cream, chiffon and banoffee pie" in p
    assert "a no-bake cheesecake is null" in p
    assert "lemon meringue" not in p


def test_a_savoury_tart_set_with_egg_and_cream_is_pie_custard():
    # Cayenne F-487: Leek and Caerphilly, Shallot Onion and Chive and the creamy leek tart were pie-savoury.
    p = _bake_rule()
    assert "quiche, any savoury tart filled with eggs and cream" in p
    assert "savoury tarts and galettes not filled with eggs and cream" in p


def test_a_baking_dish_takes_the_family_of_its_shape_and_a_choice_takes_the_first_size():
    # The live library had 13 dimensioned dishes with no family ("an ovenproof dish, about 18 x 23cm"),
    # so no recipe baked in one could be fitted: the vocabulary named tins and pans, never dishes.
    p = _bake_rule()
    assert "A baking, ovenproof, gratin or casserole dish takes the family of its shape" in p
    assert "rectangular traybake, square square, round round-cake," in p
    assert "the first size and shape it names" in p
    assert "A rectangle is traybake, never square: square only when its sides are equal" in p


def test_a_pie_plate_is_pie_a_tart_tin_is_tart_and_a_capacity_only_pie_dish_is_pie():
    # Cayenne pie tins D59.
    p = _bake_rule()
    assert "a pie plate, pie dish, pie pan or pie tin pie; a tart, flan or quiche tin tart." in p
    assert "A pie dish named only by its capacity is pie, round, with capacity_ml." in p


def test_a_tray_that_only_carries_another_tin_is_not_listed():
    # Cayenne pie tins D59: the crust's half sheet pan was only its blind-baking tray.
    p = _bake_rule()
    assert (
        "A baking sheet, tray or roasting tin that only holds another tin or dish (the pie plate set on a"
        " baking sheet, a water bath, a tray to catch drips) is not listed; list a tray only when the item"
        " bakes directly on it." in p
    )


def test_a_choice_of_any_tins_or_a_range_of_sizes_is_one_tin_the_first():
    # Cayenne pie tins D59 (F-367's first half): the rule covers every tin, not only dishes.
    p = _bake_rule()
    assert (
        'When the recipe offers a choice of tins or a range of sizes ("8-inch square or round",'
        ' "9- or 10-inch springform"), list one tin: the first size and shape it names.' in p
    )


def test_pie_savoury_needs_a_crust_or_lid_and_a_rolled_or_packed_filling_is_null():
    # Owner, 2026-11-01 (pie tins dry run): Beef Enchiladas and Perfect Nut Roast were pie-savoury.
    p = _bake_rule()
    assert "Pie-savoury is for a savoury filling only, and needs a crust or lid of pastry, suet or mashed potato" in p
    assert (
        "A filling rolled in tortillas, pasta or leaves, or packed into a loaf tin with no crust or lid"
        " (enchiladas, cannelloni, a nut roast wrapped in cabbage leaves, meatloaf), is null." in p
    )


def test_a_torte_of_layered_pastry_is_null_never_pie_shell():
    # Owner, 2026-11-01 (pie tins dry run): Friesentorte was pie-shell.
    p = _bake_rule()
    # In the closing null list the clause also turned a hazelnut meringue roulade into a sponge; in step
    # 10 it does not.
    assert (
        "Puff pastry rounds or sheets baked flat to be stacked with cream and fruit into a torte"
        " (Friesentorte, mille-feuille) are not a pie or tart crust: null." in p
    )
    assert p.index("(Friesentorte, mille-feuille)") > p.index("10. pie-shell:")


def test_filling_quote_is_asked_before_bake_type():
    # Owner, 2026-11-01: step 10 must never catch a recipe that makes a filling (Mango Pie).
    p = _bake_rule()
    assert '"filling_quote": the words of the step that puts a filling or topping into or onto' in p
    assert p.index('"filling_quote"') < p.index('"bake_type"')
    assert "If the recipe also makes anything that goes into the crust" in p


def test_meringue_is_never_sponge_meat_pies_are_savoury_and_gypsy_tart_is_custard():
    # Owner, 2026-11-01: Strawberries and Cream Pavlova was sponge, Sweet and Spicy Beef and Pork Pie
    # was pie-custard, and Gypsy Tart was pie-nut.
    p = _bake_rule()
    assert "A meringue, pavlova or dacquoise (egg whites beaten with sugar, no flour), even baked in cake tins" in p
    assert "A filling that is mostly meat is pie-savoury, never pie-custard, even when eggs bind it" in p
    assert "bacon pieces in an egg-and-cream quiche filling stay pie-custard" in p
    assert "evaporated or condensed milk whisked with sugar and baked until set (Gypsy tart)" in p
