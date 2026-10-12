"""refine() normalises heading entries (Cayenne ingredient headings design D3)."""
import logging
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from recipeparser.models import CayenneRefinement, RecipeExtraction, StructuredIngredient, TokenizedDirection

RAW = RecipeExtraction(name="Shrimp", ingredients=["For the marinade:", "1/4 tsp salt", "Day 2"], directions=["Mix."])


def _run(refinement):
    from recipeparser.core.stages.refine import refine
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        return refine(RAW, client=MagicMock())


def _refinement(ingredients, steps=("Mix.",)):
    return CayenneRefinement(
        title="Shrimp", base_servings=2, structured_ingredients=ingredients,
        tokenized_directions=[TokenizedDirection(step=n, text=t) for n, t in enumerate(steps, 1)],
    )


def test_kind_is_required():
    with pytest.raises(ValidationError):
        StructuredIngredient(id="ing_01", name="salt", fallback_string="salt")


def test_kind_rejects_other_values():
    with pytest.raises(ValidationError):
        StructuredIngredient(id="ing_01", name="salt", fallback_string="salt", kind="section")


def test_a_heading_loses_every_quantity_and_gets_a_clean_name():
    heading = StructuredIngredient(
        id="ing_03", kind="heading", amount=2, unit="day", name="Day 2", fallback_string="**Day 2:**",
        converted_amount=48, converted_unit="h", is_ai_converted=True, state="solid", line_index=2,
    )
    salt = StructuredIngredient(id="ing_02", kind="ingredient", amount=0.25, unit="tsp", name="salt",
                                fallback_string="1/4 tsp salt", line_index=1)
    result = _run(_refinement([salt, heading]))
    h = result.structured_ingredients[1]
    assert (h.amount, h.unit, h.converted_amount, h.converted_unit, h.is_ai_converted, h.state) == (None, None, None, None, False, None)
    assert h.name == "Day 2"
    assert h.fallback_string == "**Day 2:**"
    assert h.line_index == 2
    assert result.structured_ingredients[0].amount == 0.25  # an ingredient is untouched


def test_a_token_naming_a_heading_is_unwrapped_to_its_words(caplog):
    caplog.set_level(logging.WARNING, logger="recipeparser.core.stages.refine")
    heading = StructuredIngredient(id="ing_01", kind="heading", name="For the marinade", fallback_string="For the marinade:", line_index=0)
    salt = StructuredIngredient(id="ing_02", kind="ingredient", amount=0.25, unit="tsp", name="salt", fallback_string="1/4 tsp salt", line_index=1)
    result = _run(_refinement([heading, salt], steps=("Combine {{ing_01|the marinade|all}} with {{ing_02|salt|all}}.",)))
    assert result.tokenized_directions[0].text == "Combine the marinade with {{ing_02|salt|all}}."
    assert "ing_01" in caplog.text
