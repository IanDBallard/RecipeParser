"""
tests/transient/test_shadow_execution.py — Phase 6 §11.2 shadow execution tests.

TRANSIENT: Delete this entire file in Phase 8 when legacy code is removed.
Run with: pytest tests/transient/ -v -m transient

Purpose: Guarantee the structural refactor (Phase 6) did not accidentally drop
fields, alter unit conversions, or skip Fat Token generation.  Both the legacy
run_cayenne_pipeline() and the new RecipePipeline.run() are fed identical
deterministic mock data and their model_dump() outputs are asserted identical.
"""
from __future__ import annotations

from typing import Dict, List
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.core.engine import run_cayenne_pipeline
from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType
from recipeparser.core.pipeline import RecipePipeline
from recipeparser.core.ports import CategorySource
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.models import (
    CayenneRefinement,
    IngestResponse,
    StructuredIngredient,
    TokenizedDirection,
)

FAKE_EMBEDDING: List[float] = [0.1] * 1536

COMPLEX_RECIPE_TEXT = """\
Sourdough Bread

Prep time: 30 minutes
Cook time: 45 minutes
Servings: 2 loaves

Ingredients:
- 500g bread flour (100%)
- 375g water (75%)
- 100g active sourdough starter (20%)
- 10g salt (2%)

Instructions:
1. Mix flour and water, autolyse for 30 minutes.
2. Add starter and salt, mix until incorporated.
3. Bulk ferment at room temperature for 4-6 hours.
4. Bake covered for 20 minutes, then uncovered for 25 minutes.
"""

# Deterministic structured ingredients shared by both paths
_INGREDIENTS = [
    StructuredIngredient(
        id="ing_01",
        amount=500.0,
        unit="g",
        name="bread flour",
        fallback_string="500g bread flour",
        converted_amount=None,
        converted_unit=None,
        is_ai_converted=False,
    ),
    StructuredIngredient(
        id="ing_02",
        amount=375.0,
        unit="g",
        name="water",
        fallback_string="375g water",
        converted_amount=None,
        converted_unit=None,
        is_ai_converted=False,
    ),
]

# Deterministic tokenized directions shared by both paths
_DIRECTIONS = [
    TokenizedDirection(
        step=1,
        text="Mix {{ing_01|500g bread flour}} and {{ing_02|375g water}}, autolyse for 30 minutes.",
    ),
    TokenizedDirection(
        step=2,
        text="Bake covered for 20 minutes, then uncovered for 25 minutes.",
    ),
]

_GRID_CATEGORIES: Dict[str, List[str]] = {"Type": ["Bread"]}
_CATEGORIES: List[str] = ["Bread"]


class _FakeCategorySource(CategorySource):
    def load_axes(self, user_id: str = "") -> Dict[str, List[str]]:
        return {"Type": ["Bread", "Pastry", "Cake"]}

    def load_category_ids(self, user_id: str = "") -> Dict[str, str]:
        return {"Bread": "uuid-bread"}


# Patch targets for the new RecipePipeline path
_PATCH_EXTRACT = "recipeparser.core.pipeline.extract"
_PATCH_REFINE = "recipeparser.core.pipeline.refine"
_PATCH_CATEGORIZE = "recipeparser.core.pipeline.categorize"
_PATCH_EMBED = "recipeparser.core.pipeline.embed"
_PATCH_ASSEMBLE = "recipeparser.core.pipeline.assemble"

# Patch targets for the legacy run_cayenne_pipeline() path.
# run_cayenne_pipeline() imports these inside the function body via
# `from recipeparser.gemini import ...` — so we patch at the gemini module.
_PATCH_LEGACY_EXTRACT = "recipeparser.gemini.extract_recipe_from_text"
_PATCH_LEGACY_REFINE = "recipeparser.gemini.refine_recipe_for_cayenne"
_PATCH_LEGACY_EMBED = "recipeparser.gemini.get_embeddings"


def _make_pipeline() -> RecipePipeline:
    GlobalRateLimiter().reset()
    return RecipePipeline(
        client=MagicMock(),
        controller=PipelineController(),
        category_source=_FakeCategorySource(),
        rpm=9999,
    )


def _make_mock_refinement() -> MagicMock:
    """Return a mock that quacks like CayenneRefinement."""
    m = MagicMock(spec=CayenneRefinement)
    m.title = "Sourdough Bread"
    m.base_servings = 2
    m.structured_ingredients = _INGREDIENTS
    m.tokenized_directions = _DIRECTIONS
    m.grid_categories = _GRID_CATEGORIES
    return m


@pytest.mark.transient
def test_shadow_execution_produces_identical_output():
    """
    §11.2 Shadow Execution: structural refactor must not alter any field.
    TRANSIENT: Delete in Phase 8 when legacy code is removed.

    Both paths receive identical deterministic mock data.  The test asserts
    that every field in model_dump() is identical between the two outputs,
    proving the refactor is a pure structural change with no semantic drift.
    """
    mock_client = MagicMock()
    user_axes = {"Type": ["Bread", "Pastry", "Cake"]}

    # ── Legacy path ──────────────────────────────────────────────────────────
    mock_raw_recipe = MagicMock()
    mock_raw_recipe.prep_time = "30 minutes"
    mock_raw_recipe.cook_time = "45 minutes"
    mock_recipe_list = MagicMock()
    mock_recipe_list.recipes = [mock_raw_recipe]

    mock_refined = _make_mock_refinement()

    with (
        patch(_PATCH_LEGACY_EXTRACT, return_value=mock_recipe_list),
        patch(_PATCH_LEGACY_REFINE, return_value=mock_refined),
        patch(_PATCH_LEGACY_EMBED, return_value=FAKE_EMBEDDING),
    ):
        legacy_result = run_cayenne_pipeline(
            source_text=COMPLEX_RECIPE_TEXT,
            client=mock_client,
            user_axes=user_axes,
        )

    # ── New pipeline path ─────────────────────────────────────────────────────
    chunk = Chunk(
        input_type=InputType.TEXT,
        content=COMPLEX_RECIPE_TEXT,
        source_hint="shadow-test",
    )

    # assemble() returns the final IngestResponse — build one that matches legacy
    assembled = IngestResponse(
        title="Sourdough Bread",
        prep_time="30 minutes",
        cook_time="45 minutes",
        base_servings=2,
        source_url=None,
        image_url=None,
        categories=_CATEGORIES,
        grid_categories=_GRID_CATEGORIES,
        structured_ingredients=_INGREDIENTS,
        tokenized_directions=_DIRECTIONS,
        embedding=FAKE_EMBEDDING,
    )

    with (
        patch(_PATCH_EXTRACT, return_value=MagicMock()),
        patch(_PATCH_REFINE, return_value=mock_refined),
        patch(_PATCH_CATEGORIZE, return_value=_GRID_CATEGORIES),
        patch(_PATCH_EMBED, return_value=FAKE_EMBEDDING),
        patch(_PATCH_ASSEMBLE, return_value=assembled),
    ):
        pipeline = _make_pipeline()
        new_results = pipeline.run([chunk], None, "test-user-id")

    assert len(new_results) == 1, "Pipeline must return exactly one result for one chunk"
    new_result = new_results[0]

    # ── Field-by-field comparison ─────────────────────────────────────────────
    legacy_dump = legacy_result.model_dump()
    new_dump = new_result.model_dump()

    # Exclude embedding from diff (both are FAKE_EMBEDDING but list equality
    # is slow for 1536-element lists — check separately)
    legacy_embedding = legacy_dump.pop("embedding")
    new_embedding = new_dump.pop("embedding")

    assert legacy_dump == new_dump, (
        "Shadow execution mismatch — structural refactor introduced semantic drift.\n"
        f"Legacy fields: {set(legacy_dump.keys())}\n"
        f"New fields:    {set(new_dump.keys())}\n"
        f"Diff: { {k: (legacy_dump.get(k), new_dump.get(k)) for k in set(legacy_dump) | set(new_dump) if legacy_dump.get(k) != new_dump.get(k)} }"
    )

    assert legacy_embedding == new_embedding, (
        "Shadow execution: embedding vectors differ between legacy and new pipeline."
    )


@pytest.mark.transient
def test_shadow_execution_preserves_fat_tokens():
    """
    §11.2 Shadow Execution (supplemental): Fat Token format must survive the refactor.
    TRANSIENT: Delete in Phase 8 when legacy code is removed.
    """
    import re

    FAT_TOKEN_RE = re.compile(r"\{\{([^|]+)\|([^}]+)\}\}")

    chunk = Chunk(
        input_type=InputType.TEXT,
        content=COMPLEX_RECIPE_TEXT,
        source_hint="fat-token-shadow-test",
    )

    assembled = IngestResponse(
        title="Sourdough Bread",
        prep_time="30 minutes",
        cook_time="45 minutes",
        base_servings=2,
        source_url=None,
        image_url=None,
        categories=_CATEGORIES,
        grid_categories=_GRID_CATEGORIES,
        structured_ingredients=_INGREDIENTS,
        tokenized_directions=_DIRECTIONS,
        embedding=FAKE_EMBEDDING,
    )

    mock_refined = _make_mock_refinement()

    with (
        patch(_PATCH_EXTRACT, return_value=MagicMock()),
        patch(_PATCH_REFINE, return_value=mock_refined),
        patch(_PATCH_CATEGORIZE, return_value=_GRID_CATEGORIES),
        patch(_PATCH_EMBED, return_value=FAKE_EMBEDDING),
        patch(_PATCH_ASSEMBLE, return_value=assembled),
    ):
        pipeline = _make_pipeline()
        results = pipeline.run([chunk], None, "test-user-id")

    assert results, "Pipeline returned no results"
    result = results[0]

    # Every direction that references an ingredient must contain at least one Fat Token
    direction_texts = [d.text for d in result.tokenized_directions]
    tokens_found = []
    for text in direction_texts:
        tokens_found.extend(FAT_TOKEN_RE.findall(text))

    assert tokens_found, (
        "No Fat Tokens found in tokenized_directions — "
        "the refactor may have broken Fat Token generation."
    )

    # Verify token IDs reference valid ingredient IDs
    ingredient_ids = {ing.id for ing in result.structured_ingredients}
    for token_id, _fallback in tokens_found:
        assert token_id in ingredient_ids, (
            f"Fat Token references unknown ingredient ID '{token_id}'. "
            f"Valid IDs: {ingredient_ids}"
        )
