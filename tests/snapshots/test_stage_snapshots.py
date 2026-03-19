"""
tests/snapshots/test_stage_snapshots.py — Syrupy snapshot tests for stage outputs.

These tests lock in the *shape* of each stage's output so that future
refactors cannot silently change the data contract.  They use the same
mock fixtures as the gate tests — no network access required.

Run with --snapshot-update to regenerate snapshots after an intentional
schema change.
"""
from __future__ import annotations

from typing import List
from unittest.mock import MagicMock, patch

import pytest
from syrupy.assertion import SnapshotAssertion

from recipeparser.models import (
    CayenneRefinement,
    RecipeExtraction,
    RecipeList,
    StructuredIngredient,
    TokenizedDirection,
)


# ---------------------------------------------------------------------------
# Shared fixture helpers (duplicated from gate tests to keep snapshots
# self-contained — avoids cross-module import coupling)
# ---------------------------------------------------------------------------

def _make_ingredient(id_: str = "ing_01", name: str = "flour") -> StructuredIngredient:
    return StructuredIngredient(
        id=id_,
        amount=1.5,
        unit="cups",
        name=name,
        fallback_string=f"1.5 cups {name}",
        converted_amount=None,
        converted_unit=None,
        is_ai_converted=False,
    )


def _make_refinement(title: str = "Test Cake") -> CayenneRefinement:
    ing = _make_ingredient()
    return CayenneRefinement(
        title=title,
        base_servings=4,
        structured_ingredients=[ing],
        tokenized_directions=[
            TokenizedDirection(
                step=1,
                text="Mix {{ing_01|1.5 cups flour}} until smooth.",
            )
        ],
        grid_categories={"Cuisine": ["Italian"], "Meal Type": ["Dessert"]},
    )


def _make_raw_recipe(name: str = "Test Cake") -> RecipeExtraction:
    return RecipeExtraction(
        name=name,
        servings="4",
        prep_time="10 mins",
        cook_time="30 mins",
        ingredients=["1.5 cups flour"],
        directions=["Mix until smooth."],
    )


# ---------------------------------------------------------------------------
# Snapshot: extract stage output shape
# ---------------------------------------------------------------------------

def test_extract_output_shape(snapshot: SnapshotAssertion) -> None:
    """Lock in the list-of-RecipeExtraction shape returned by extract()."""
    from recipeparser.core.stages.extract import extract

    raw = _make_raw_recipe()
    mock_result = RecipeList(recipes=[raw])
    with patch("recipeparser.core.stages.extract.extract_recipes", return_value=mock_result):
        result = extract("Some text about cooking.", client=MagicMock())

    # Serialize to dict for stable snapshot comparison
    serialized = [r.model_dump() for r in result]
    assert serialized == snapshot


# ---------------------------------------------------------------------------
# Snapshot: refine stage output shape
# ---------------------------------------------------------------------------

def test_refine_output_shape(snapshot: SnapshotAssertion) -> None:
    """Lock in the CayenneRefinement shape returned by refine()."""
    from recipeparser.core.stages.refine import refine

    raw = _make_raw_recipe()
    refinement = _make_refinement()
    with patch(
        "recipeparser.core.stages.refine.refine_recipe_for_cayenne",
        return_value=refinement,
    ):
        result = refine(raw, client=MagicMock())

    assert result.model_dump() == snapshot


# ---------------------------------------------------------------------------
# Snapshot: categorize stage output shape
# ---------------------------------------------------------------------------

def test_categorize_output_shape(snapshot: SnapshotAssertion) -> None:
    """Lock in the Dict[str, List[str]] shape returned by categorize()."""
    from recipeparser.core.stages.categorize import categorize

    refinement = _make_refinement()
    user_axes = {"Cuisine": ["Italian", "French"], "Meal Type": ["Dessert", "Breakfast"]}
    result = categorize(refinement, user_axes=user_axes)

    assert result == snapshot


# ---------------------------------------------------------------------------
# Snapshot: embed stage output shape
# ---------------------------------------------------------------------------

def test_embed_output_shape(snapshot: SnapshotAssertion) -> None:
    """Lock in the List[float] shape (length + first/last values) from embed()."""
    from recipeparser.core.stages.embed import embed

    refinement = _make_refinement()
    # Use a deterministic fake vector so the snapshot is stable
    fake_vector: List[float] = [round(i * 0.001, 6) for i in range(1536)]
    with patch("recipeparser.core.stages.embed.get_embeddings", return_value=fake_vector):
        result = embed(refinement, client=MagicMock())

    # Snapshot the shape metadata, not the full 1536-element list
    shape_summary = {
        "length": len(result),
        "first_5": result[:5],
        "last_5": result[-5:],
        "all_floats": all(isinstance(v, float) for v in result),
    }
    assert shape_summary == snapshot


# ---------------------------------------------------------------------------
# Snapshot: assemble stage output shape
# ---------------------------------------------------------------------------

def test_assemble_output_shape(snapshot: SnapshotAssertion) -> None:
    """Lock in the IngestResponse shape returned by assemble()."""
    from recipeparser.core.stages.assemble import assemble

    refinement = _make_refinement()
    # Use a short deterministic embedding for the snapshot
    embedding: List[float] = [0.1, 0.2, 0.3]  # intentionally short for readability
    grid = {"Cuisine": ["Italian"], "Meal Type": ["Dessert"]}

    # Patch assemble to accept any embedding length (snapshot test only)
    result = assemble(
        recipe=refinement,
        embedding=embedding,
        source_url="https://example.com/cake",
        image_url=None,
        grid_categories=grid,
    )

    # Exclude the full embedding from the snapshot (covered by embed snapshot)
    dumped = result.model_dump()
    dumped["embedding"] = f"<vector len={len(dumped['embedding'])}>"
    assert dumped == snapshot
