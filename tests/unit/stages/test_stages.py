"""
tests/unit/stages/test_stages.py — Phase 1 gate tests for core stage modules.

These tests use unittest.mock to patch the Gemini API calls so no network
access is required.  Each test validates the contract of one stage function:
correct return type, correct error handling, and correct pure-function logic.

TID rule: no imports from recipeparser.io or recipeparser.adapters.
"""
from __future__ import annotations

from typing import List
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.models import (
    CayenneRefinement,
    RecipeExtraction,
    RecipeList,
    StructuredIngredient,
    TokenizedDirection,
)


# ---------------------------------------------------------------------------
# Shared fixtures
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
# Gate 1 — extract
# ---------------------------------------------------------------------------

class TestExtract:
    """extract() wraps gemini.extract_recipes / extract_recipe_from_text."""

    def test_raises_on_empty_chunk(self) -> None:
        from recipeparser.core.stages.extract import extract
        with pytest.raises(ValueError, match="non-empty"):
            extract("", client=MagicMock())

    def test_raises_on_whitespace_only_chunk(self) -> None:
        from recipeparser.core.stages.extract import extract
        with pytest.raises(ValueError, match="non-empty"):
            extract("   \n\t  ", client=MagicMock())

    def test_returns_empty_list_when_gemini_returns_none(self) -> None:
        from recipeparser.core.stages.extract import extract
        with patch("recipeparser.core.stages.extract.extract_recipes", return_value=None):
            result = extract("Some text about cooking.", client=MagicMock())
        assert result == []

    def test_returns_list_of_recipe_extractions(self) -> None:
        from recipeparser.core.stages.extract import extract
        raw = _make_raw_recipe()
        mock_result = RecipeList(recipes=[raw])
        with patch("recipeparser.core.stages.extract.extract_recipes", return_value=mock_result):
            result = extract("Some text about cooking.", client=MagicMock())
        assert len(result) == 1
        assert isinstance(result[0], RecipeExtraction)
        assert result[0].name == "Test Cake"

    def test_plain_text_mode_calls_extract_recipe_from_text(self) -> None:
        from recipeparser.core.stages.extract import extract
        raw = _make_raw_recipe()
        mock_result = RecipeList(recipes=[raw])
        with patch(
            "recipeparser.core.stages.extract.extract_recipe_from_text",
            return_value=mock_result,
        ) as mock_fn:
            result = extract("Some text.", client=MagicMock(), plain_text_mode=True)
        mock_fn.assert_called_once()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Gate 2 — refine
# ---------------------------------------------------------------------------

class TestRefine:
    """refine() wraps gemini.refine_recipe_for_cayenne and validates Fat Tokens."""

    def test_raises_when_gemini_returns_none(self) -> None:
        from recipeparser.core.stages.refine import refine
        raw = _make_raw_recipe()
        with patch(
            "recipeparser.core.stages.refine.refine_recipe_for_cayenne",
            return_value=None,
        ):
            with pytest.raises(ValueError, match="Gemini returned None"):
                refine(raw, client=MagicMock())

    def test_returns_cayenne_refinement_on_success(self) -> None:
        from recipeparser.core.stages.refine import refine
        raw = _make_raw_recipe()
        refinement = _make_refinement()
        with patch(
            "recipeparser.core.stages.refine.refine_recipe_for_cayenne",
            return_value=refinement,
        ):
            result = refine(raw, client=MagicMock())
        assert isinstance(result, CayenneRefinement)
        assert result.title == "Test Cake"

    def test_raises_on_dangling_fat_token(self) -> None:
        """A Fat Token referencing a non-existent ingredient ID must raise ValueError."""
        from recipeparser.core.stages.refine import refine
        raw = _make_raw_recipe()
        # Direction references ing_99 but only ing_01 exists
        bad_refinement = CayenneRefinement(
            title="Bad Recipe",
            base_servings=2,
            structured_ingredients=[_make_ingredient("ing_01")],
            tokenized_directions=[
                TokenizedDirection(
                    step=1,
                    text="Add {{ing_99|1 cup mystery}} and stir.",
                )
            ],
            grid_categories={},
        )
        with patch(
            "recipeparser.core.stages.refine.refine_recipe_for_cayenne",
            return_value=bad_refinement,
        ):
            with pytest.raises(ValueError, match="ing_99"):
                refine(raw, client=MagicMock())


# ---------------------------------------------------------------------------
# Gate 3 — categorize
# ---------------------------------------------------------------------------

class TestCategorize:
    """categorize() is a pure function — no mocking needed."""

    def test_returns_empty_dict_when_no_user_axes(self) -> None:
        from recipeparser.core.stages.categorize import categorize
        refinement = _make_refinement()
        result = categorize(refinement, user_axes={})
        assert result == {}

    def test_filters_to_valid_tags_only(self) -> None:
        from recipeparser.core.stages.categorize import categorize
        refinement = _make_refinement()
        # grid_categories has {"Cuisine": ["Italian"], "Meal Type": ["Dessert"]}
        # user_axes only allows "Pasta" for Cuisine — "Italian" is not valid
        user_axes = {"Cuisine": ["French", "Pasta"], "Meal Type": ["Dessert", "Breakfast"]}
        result = categorize(refinement, user_axes=user_axes)
        assert "Cuisine" not in result  # "Italian" filtered out
        assert result.get("Meal Type") == ["Dessert"]

    def test_returns_matching_tags_for_valid_axis(self) -> None:
        from recipeparser.core.stages.categorize import categorize
        refinement = _make_refinement()
        user_axes = {"Cuisine": ["Italian", "French"], "Meal Type": ["Dessert"]}
        result = categorize(refinement, user_axes=user_axes)
        assert result["Cuisine"] == ["Italian"]
        assert result["Meal Type"] == ["Dessert"]

    def test_ignores_axes_not_in_grid_categories(self) -> None:
        from recipeparser.core.stages.categorize import categorize
        refinement = _make_refinement()
        user_axes = {"Protein": ["Chicken", "Beef"]}  # not in grid_categories
        result = categorize(refinement, user_axes=user_axes)
        assert result == {}


# ---------------------------------------------------------------------------
# Gate 4 — embed
# ---------------------------------------------------------------------------

class TestEmbed:
    """embed() wraps gemini.get_embeddings and validates dimensionality."""

    def test_returns_1536_dim_vector(self) -> None:
        from recipeparser.core.stages.embed import embed
        refinement = _make_refinement()
        fake_vector: List[float] = [0.1] * 1536
        with patch("recipeparser.core.stages.embed.get_embeddings", return_value=fake_vector):
            result = embed(refinement, client=MagicMock())
        assert len(result) == 1536
        assert isinstance(result[0], float)

    def test_raises_on_wrong_dimensionality(self) -> None:
        from recipeparser.core.stages.embed import embed
        refinement = _make_refinement()
        wrong_vector: List[float] = [0.1] * 512  # wrong dim
        with patch("recipeparser.core.stages.embed.get_embeddings", return_value=wrong_vector):
            with pytest.raises(RuntimeError, match="1536"):
                embed(refinement, client=MagicMock())

    def test_embedding_input_includes_title_and_ingredients(self) -> None:
        """Verify the text sent to get_embeddings contains title + fallback strings."""
        from recipeparser.core.stages.embed import embed
        refinement = _make_refinement()
        fake_vector: List[float] = [0.0] * 1536
        with patch(
            "recipeparser.core.stages.embed.get_embeddings", return_value=fake_vector
        ) as mock_fn:
            embed(refinement, client=MagicMock())
        call_text: str = mock_fn.call_args[0][0]
        assert "Test Cake" in call_text
        assert "1.5 cups flour" in call_text


# ---------------------------------------------------------------------------
# Gate 5 — assemble
# ---------------------------------------------------------------------------

class TestAssemble:
    """assemble() is a pure function — no mocking needed."""

    def test_returns_ingest_response(self) -> None:
        from recipeparser.core.stages.assemble import assemble
        from recipeparser.models import IngestResponse
        refinement = _make_refinement()
        embedding = [0.1] * 1536
        grid = {"Cuisine": ["Italian"], "Meal Type": ["Dessert"]}
        result = assemble(
            recipe=refinement,
            embedding=embedding,
            source_url="https://example.com/cake",
            image_url=None,
            grid_categories=grid,
        )
        assert isinstance(result, IngestResponse)
        assert result.title == "Test Cake"
        assert result.source_url == "https://example.com/cake"
        assert result.image_url is None

    def test_flattens_grid_categories_to_categories_list(self) -> None:
        from recipeparser.core.stages.assemble import assemble
        refinement = _make_refinement()
        embedding = [0.0] * 1536
        grid = {"Cuisine": ["Italian"], "Meal Type": ["Dessert"]}
        result = assemble(
            recipe=refinement,
            embedding=embedding,
            source_url=None,
            image_url=None,
            grid_categories=grid,
        )
        # Flat list must contain all tags from all axes
        assert set(result.categories) == {"Italian", "Dessert"}

    def test_empty_grid_produces_empty_categories(self) -> None:
        from recipeparser.core.stages.assemble import assemble
        refinement = _make_refinement()
        embedding = [0.0] * 1536
        result = assemble(
            recipe=refinement,
            embedding=embedding,
            source_url=None,
            image_url=None,
            grid_categories={},
        )
        assert result.categories == []
        assert result.grid_categories == {}

    def test_embedding_is_preserved_exactly(self) -> None:
        from recipeparser.core.stages.assemble import assemble
        refinement = _make_refinement()
        embedding = [float(i) for i in range(1536)]
        result = assemble(
            recipe=refinement,
            embedding=embedding,
            source_url=None,
            image_url=None,
            grid_categories={},
        )
        assert result.embedding == embedding

    def test_image_url_is_passed_through(self) -> None:
        """image_url must survive verbatim into IngestResponse — regression guard."""
        from recipeparser.core.stages.assemble import assemble
        refinement = _make_refinement()
        embedding = [0.0] * 1536
        image_url = "https://storage.supabase.co/bucket/hero.jpg"
        result = assemble(
            recipe=refinement,
            embedding=embedding,
            source_url=None,
            image_url=image_url,
            grid_categories={},
        )
        assert result.image_url == image_url

    def test_source_url_none_for_file_input(self) -> None:
        """source_url=None (file/text input) must be preserved as None, not coerced."""
        from recipeparser.core.stages.assemble import assemble
        refinement = _make_refinement()
        embedding = [0.0] * 1536
        result = assemble(
            recipe=refinement,
            embedding=embedding,
            source_url=None,
            image_url=None,
            grid_categories={},
        )
        assert result.source_url is None

    def test_prep_and_cook_time_are_passed_through(self) -> None:
        """prep_time and cook_time must come from explicit params, not CayenneRefinement.

        Regression guard: CayenneRefinement has no prep_time/cook_time fields.
        Accessing them on the recipe object would raise AttributeError — this test
        ensures the assemble() fix (explicit params) is never reverted.
        """
        from recipeparser.core.stages.assemble import assemble
        refinement = _make_refinement()
        embedding = [0.0] * 1536
        result = assemble(
            recipe=refinement,
            embedding=embedding,
            source_url=None,
            image_url=None,
            grid_categories={},
            prep_time="15 mins",
            cook_time="45 mins",
        )
        assert result.prep_time == "15 mins"
        assert result.cook_time == "45 mins"

    def test_prep_and_cook_time_default_to_none(self) -> None:
        """When omitted, prep_time and cook_time must default to None (not raise)."""
        from recipeparser.core.stages.assemble import assemble
        refinement = _make_refinement()
        embedding = [0.0] * 1536
        result = assemble(
            recipe=refinement,
            embedding=embedding,
            source_url=None,
            image_url=None,
            grid_categories={},
            # prep_time and cook_time intentionally omitted
        )
        assert result.prep_time is None
        assert result.cook_time is None


# ---------------------------------------------------------------------------
# Gate 6 — ProgressCallback + notify_progress
# ---------------------------------------------------------------------------

class TestProgressCallback:
    """ProgressCallback Protocol and PipelineController.notify_progress()."""

    def test_progress_callback_protocol_is_satisfied_by_lambda(self) -> None:
        from recipeparser.core.fsm import ProgressCallback
        calls: list = []

        def my_cb(stage: str, completed: int, total: int) -> None:
            calls.append((stage, completed, total))

        assert isinstance(my_cb, ProgressCallback)

    def test_notify_progress_fires_callback(self) -> None:
        from recipeparser.core.fsm import PipelineController
        calls: list = []

        def cb(stage: str, completed: int, total: int) -> None:
            calls.append((stage, completed, total))

        ctrl = PipelineController(on_progress=cb)
        ctrl.notify_progress("EXTRACTING", 3, 10)
        assert calls == [("EXTRACTING", 3, 10)]

    def test_notify_progress_is_noop_without_callback(self) -> None:
        from recipeparser.core.fsm import PipelineController
        ctrl = PipelineController()  # no on_progress
        # Must not raise
        ctrl.notify_progress("EXTRACTING", 0, 0)

    def test_notify_progress_swallows_callback_exception(self) -> None:
        from recipeparser.core.fsm import PipelineController

        def bad_cb(stage: str, completed: int, total: int) -> None:
            raise RuntimeError("adapter bug")

        ctrl = PipelineController(on_progress=bad_cb)
        # Must not propagate the exception
        ctrl.notify_progress("EXTRACTING", 1, 5)
