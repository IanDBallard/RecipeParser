"""refine() validates line_index against the raw ingredient list (spec 4.3)."""
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.models import (
    CayenneRefinement,
    RecipeExtraction,
    StructuredIngredient,
    TokenizedDirection,
)


def _raw(n_lines: int) -> RecipeExtraction:
    return RecipeExtraction(
        name="Cake",
        ingredients=[f"{i} cups thing {i}" for i in range(n_lines)],
        directions=["Mix."],
    )


def _ing(id_: str, line_index):
    return StructuredIngredient(
        id=id_, amount=1.0, unit="cup", name="thing",
        fallback_string="1 cup thing", line_index=line_index,
    )


def _refinement(indices):
    return CayenneRefinement(
        title="Cake",
        base_servings=4,
        structured_ingredients=[_ing(f"ing_{i:02d}", idx) for i, idx in enumerate(indices, 1)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix.")],
    )


def _run(raw, refinement):
    from recipeparser.core.stages.refine import refine
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        return refine(raw, client=MagicMock())


def test_line_index_defaults_to_none():
    ing = StructuredIngredient(id="ing_01", name="x", fallback_string="x")
    assert ing.line_index is None


def test_valid_indices_pass():
    result = _run(_raw(3), _refinement([0, 1, 2]))
    assert [i.line_index for i in result.structured_ingredients] == [0, 1, 2]


def test_none_indices_pass():
    result = _run(_raw(2), _refinement([None, None]))
    assert len(result.structured_ingredients) == 2


def test_header_line_skipped_is_fine():
    # 3 raw lines, header at 1 produces no entry
    result = _run(_raw(3), _refinement([0, 2]))
    assert [i.line_index for i in result.structured_ingredients] == [0, 2]


def test_out_of_range_raises():
    with pytest.raises(ValueError, match="line_index 5"):
        _run(_raw(3), _refinement([0, 5]))


def test_negative_raises():
    with pytest.raises(ValueError, match="line_index -1"):
        _run(_raw(3), _refinement([-1]))


def test_duplicate_raises():
    with pytest.raises(ValueError, match="line_index 1 .*twice"):
        _run(_raw(3), _refinement([1, 1]))
