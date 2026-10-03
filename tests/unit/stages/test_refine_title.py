"""REFINE stores every title in house title case (the title-case titles ruling, 2026-10-03)."""
from unittest.mock import MagicMock, patch

from recipeparser.core.pipeline import _pre_parsed_to_refinement
from recipeparser.core.stages.refine import refine
from recipeparser.models import CayenneRecipe, CayenneRefinement, RecipeExtraction, TokenizedDirection

_PATCH = "recipeparser.core.stages.refine.refine_recipe_for_cayenne"

RAW = RecipeExtraction(name="CHICKEN TIKKA MASALA", ingredients=[], directions=["Cook."])


def _refinement(title: str) -> CayenneRefinement:
    return CayenneRefinement(
        title=title, base_servings=4, structured_ingredients=[],
        tokenized_directions=[TokenizedDirection(step=1, text="Cook.")],
    )


def test_an_all_caps_title_is_stored_in_title_case():
    with patch(_PATCH, return_value=_refinement("CHICKEN TIKKA MASALA")):
        result = refine(RAW, client=MagicMock())
    assert result.title == "Chicken Tikka Masala"


def test_a_mixed_case_title_is_normalised_too():
    with patch(_PATCH, return_value=_refinement("chicken tikka Masala with BBQ naan")):
        result = refine(RAW, client=MagicMock())
    assert result.title == "Chicken Tikka Masala with BBQ Naan"


def test_a_paprika_restore_skips_refine_and_is_still_title_cased():
    pr = CayenneRecipe(
        title="CHICKEN TIKKA MASALA", structured_ingredients=[],
        tokenized_directions=[TokenizedDirection(step=1, text="Cook.")],
    )
    assert _pre_parsed_to_refinement(pr).title == "Chicken Tikka Masala"
