"""refine() makes every ingredient id unique and non-empty (Cayenne Fix Roadmap F-194).

The id is a key the kitchen loops over, so Cayenne refuses a recipe whose ids repeat or are blank
("structured_ingredients has missing or duplicate ids"). Ids are unique today only because the
prompt asks for them; these tests hold refine() to it.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.models import (
    CayenneRefinement,
    RecipeExtraction,
    StructuredIngredient,
    TokenizedDirection,
)

RAW = RecipeExtraction(name="Cake", ingredients=["1 cup flour", "1 cup sugar", "2 eggs"], directions=["Mix."])


def _ing(id_: str, name: str = "thing") -> StructuredIngredient:
    return StructuredIngredient(id=id_, amount=1.0, unit="cup", name=name, fallback_string=f"1 cup {name}")


def _refinement(ids, step_text="Mix.") -> CayenneRefinement:
    return CayenneRefinement(
        title="Cake",
        base_servings=4,
        structured_ingredients=[_ing(i, f"thing {n}") for n, i in enumerate(ids)],
        tokenized_directions=[TokenizedDirection(step=1, text=step_text)],
    )


def _run(refinement):
    from recipeparser.core.stages.refine import refine
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        return refine(RAW, client=MagicMock())


def _ids(result):
    return [i.id for i in result.structured_ingredients]


def test_unique_ids_are_left_alone():
    result = _run(_refinement(["ing_01", "ing_02", "ing_03"], "Mix {{ing_01|the flour}} and {{ing_02|sugar}}."))
    assert _ids(result) == ["ing_01", "ing_02", "ing_03"]


def test_ids_need_not_be_sequential():
    # The id is a key, not a position: a gap or an odd name is a good recipe.
    result = _run(_refinement(["ing_03", "flour", "ing_10"]))
    assert _ids(result) == ["ing_03", "flour", "ing_10"]


def test_an_unreferenced_duplicate_is_renumbered(caplog):
    caplog.set_level(logging.WARNING, logger="recipeparser.core.stages.refine")
    result = _run(_refinement(["ing_01", "ing_02", "ing_02"], "Mix {{ing_01|the flour}}."))
    assert _ids(result) == ["ing_01", "ing_02", "ing_03"]
    assert "ing_02" in caplog.text and "ing_03" in caplog.text


def test_a_fresh_id_never_collides_with_an_existing_one():
    result = _run(_refinement(["ing_03", "ing_03", "ing_04"]))
    assert _ids(result) == ["ing_03", "ing_05", "ing_04"]
    assert len(set(_ids(result))) == 3


def test_a_blank_id_is_given_one(caplog):
    caplog.set_level(logging.WARNING, logger="recipeparser.core.stages.refine")
    result = _run(_refinement(["ing_01", "", "  "], "Mix {{ing_01|the flour}}."))
    assert _ids(result) == ["ing_01", "ing_02", "ing_03"]
    assert "blank" in caplog.text


def test_a_referenced_duplicate_is_refused():
    # A token naming a repeated id cannot be told which ingredient it meant, so renumbering would
    # guess; the recipe goes to the chunk's error boundary instead of being stored wrong.
    with pytest.raises(ValueError, match="ing_02"):
        _run(_refinement(["ing_01", "ing_02", "ing_02"], "Mix {{ing_02|the sugar}}."))


def test_a_renumbered_recipe_still_passes_the_token_check():
    result = _run(_refinement(["ing_01", "ing_01", "ing_02"], "Mix {{ing_02|the sugar}}."))
    assert _ids(result) == ["ing_01", "ing_03", "ing_02"]
    assert "{{ing_02|the sugar}}" in result.tokenized_directions[0].text
