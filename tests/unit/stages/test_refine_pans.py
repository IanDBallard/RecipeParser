"""refine() keeps good tins, bake type and core flags and drops bad ones (Cayenne pan-aware scaling)."""

import logging
from unittest.mock import MagicMock, patch

from recipeparser.models import (
    CayenneRefinement,
    PanDims,
    RecipeExtraction,
    RecipePan,
    StructuredIngredient,
    TokenizedDirection,
)

RAW = RecipeExtraction(
    name="Sponge",
    ingredients=["1 cup flour"],
    directions=[
        "Grease two 20cm sandwich tins.",
        "Bake for 25 minutes.",
        "Fry the onion in oil.",
        "Heat the oil to 180°C.",
        "Bake until it reads 93°C in the centre.",
    ],
)
FLOUR = StructuredIngredient(
    id="ing_01", kind="ingredient", amount=1, unit="cup", name="flour", fallback_string="1 cup flour", line_index=0
)
PAN = RecipePan(
    id="pan_01", raw="two 20cm sandwich tins", count=2, shape="round", family="round-cake", dims_mm=PanDims(d=200)
)


def _run(steps, pans=(), **kw):
    from recipeparser.core.stages.refine import refine

    refinement = CayenneRefinement(
        title="Sponge",
        base_servings=4,
        structured_ingredients=[FLOUR],
        tokenized_directions=[TokenizedDirection(step=n, text=t) for n, t in enumerate(steps, 1)],
        pans=list(pans),
        **kw,
    )
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        return refine(RAW, client=MagicMock())


def test_a_sized_tin_its_token_and_the_bake_are_kept():
    steps = ["Grease {{pan_01|two 20cm sandwich tins}}.", "Bake for 25 minutes.", "Fry the onion in oil."]
    result = _run(steps, pans=[PAN], bake_type="sponge", bake_step=1)
    assert [p.id for p in result.pans] == ["pan_01"]
    assert result.pans[0].count == 2
    assert result.tokenized_directions[0].text == steps[0]
    assert result.bake_type == "sponge"
    assert result.bake_step == 1


def test_a_tin_token_with_no_tin_is_unwrapped_and_the_recipe_kept(caplog):
    with caplog.at_level(logging.WARNING):
        result = _run(["Grease {{pan_02|a tin}}.", "Bake for 25 minutes.", "Fry the onion in oil."])
    assert result.tokenized_directions[0].text == "Grease a tin."
    assert "pan_02" in caplog.text


def test_a_core_flag_on_an_oil_step_is_removed_and_the_token_kept(caplog):
    with caplog.at_level(logging.WARNING):
        result = _run(["Grease the tins.", "Bake.", "Heat the oil to {{temp:180C,core|180°C}}."])
    assert result.tokenized_directions[2].text == "Heat the oil to {{temp:180C|180°C}}."
    assert "core flag removed" in caplog.text


def test_a_core_flag_on_an_oven_value_does_not_cost_the_token():
    result = _run(["Preheat to {{temp:180C,oven,core|180°C}}.", "Bake.", "Fry."])
    assert result.tokenized_directions[0].text == "Preheat to {{temp:180C,oven|180°C}}."


def test_an_unknown_bake_type_is_dropped_with_its_step_and_the_recipe_kept(caplog):
    with caplog.at_level(logging.WARNING):
        result = _run(["Grease the tins.", "Bake.", "Fry."], bake_type="souffle", bake_step=1)
    assert result.bake_type is None
    assert result.bake_step is None
    assert result.title
    assert "bake type 'souffle' dropped" in caplog.text


def test_a_bake_step_on_a_heading_is_nulled():
    result = _run(["**Cake**", "Bake."], bake_type="sponge", bake_step=0)
    assert result.bake_type == "sponge"
    assert result.bake_step is None


def test_an_out_of_range_bake_step_is_nulled():
    result = _run(["Grease the tins.", "Bake."], bake_type="sponge", bake_step=9)
    assert result.bake_step is None


def test_a_valid_core_temperature_survives():
    steps = ["Grease the tins.", "Bake.", "Fry.", "Heat.", "Bake until it reads {{temp:93C,core|93°C}} in the centre."]
    result = _run(steps)
    assert result.tokenized_directions[4].text == steps[4]
