"""A Fat Token whose id differs from an ingredient's only by zero-padding names that ingredient (#149).

Brian's Curry Guy Bible run lost a whole chunk to "Fat Token '{{ing_1|...}}' in step 2 references
unknown ingredient ID 'ing_1'. Valid IDs: ['ing_01', ...]". The model meant ing_01.
"""
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.models import CayenneRefinement, RecipeExtraction, StructuredIngredient, TokenizedDirection

RAW = RecipeExtraction(name="Wings", ingredients=["1 kg wings", "2 tbsp oil"], directions=["Toss the wings in the oil."])
WINGS = StructuredIngredient(id="ing_01", kind="ingredient", amount=1, unit="kg", name="wings",
                             fallback_string="1 kg wings", line_index=0)
OIL = StructuredIngredient(id="ing_02", kind="ingredient", amount=2, unit="tbsp", name="oil",
                           fallback_string="2 tbsp oil", line_index=1)


def _refine(step: str):
    from recipeparser.core.stages.refine import refine
    refinement = CayenneRefinement(
        title="Wings", base_servings=4, structured_ingredients=[WINGS, OIL],
        tokenized_directions=[TokenizedDirection(step=1, text=step)],
    )
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        return refine(RAW, client=MagicMock())


def test_an_unpadded_id_is_read_as_the_padded_ingredient():
    result = _refine("Toss the {{ing_1|wings}} in the {{ing_2|oil|all}}.")
    assert result.tokenized_directions[0].text == "Toss the {{ing_01|wings}} in the {{ing_02|oil|all}}."


def test_an_over_padded_id_is_read_too():
    result = _refine("Toss the {{ing_001|wings}} in the oil.")
    assert result.tokenized_directions[0].text == "Toss the {{ing_01|wings}} in the oil."


def test_an_id_with_no_matching_ingredient_still_raises():
    with pytest.raises(ValueError, match="unknown ingredient ID 'ing_3'"):
        _refine("Toss the {{ing_3|wings}} in the oil.")
