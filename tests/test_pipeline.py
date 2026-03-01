"""Tests for recipeparser.pipeline — deduplication and PipelineContext."""
import pytest

from tests.conftest import make_recipe
from recipeparser.pipeline import deduplicate_recipes, PipelineContext


# ---------------------------------------------------------------------------
# deduplicate_recipes
# ---------------------------------------------------------------------------

class TestDeduplicateRecipes:

    def test_no_duplicates_unchanged(self):
        recipes = [make_recipe("Pasta"), make_recipe("Salad"), make_recipe("Soup")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 3

    def test_exact_duplicate_removed(self):
        recipes = [make_recipe("Pasta"), make_recipe("Pasta")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_case_insensitive_dedup(self):
        recipes = [
            make_recipe("Chocolate Cake"),
            make_recipe("chocolate cake"),
            make_recipe("CHOCOLATE CAKE"),
        ]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_leading_trailing_whitespace_normalised(self):
        recipes = [make_recipe("  Banana Bread  "), make_recipe("Banana Bread")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_first_occurrence_kept(self):
        r1 = make_recipe("Omelette", photo="omelette1.jpg")
        r2 = make_recipe("Omelette", photo="omelette2.jpg")
        result = deduplicate_recipes([r1, r2])
        assert result[0].photo_filename == "omelette1.jpg"

    def test_empty_list(self):
        assert deduplicate_recipes([]) == []

    def test_distinct_recipes_all_kept(self):
        recipes = [make_recipe(f"Recipe {i}") for i in range(10)]
        result = deduplicate_recipes(recipes)
        assert len(result) == 10


# ---------------------------------------------------------------------------
# PipelineContext
# ---------------------------------------------------------------------------

class TestPipelineContext:

    def test_context_stores_all_fields(self):
        import threading
        from unittest.mock import MagicMock

        client = MagicMock()
        sem = threading.Semaphore(5)
        ctx = PipelineContext(
            client=client,
            semaphore=sem,
            units="metric",
            category_tree=[("Soup", None)],
            paprika_cats=["Soup"],
        )
        assert ctx.client is client
        assert ctx.semaphore is sem
        assert ctx.units == "metric"
        assert ctx.category_tree == [("Soup", None)]
        assert ctx.paprika_cats == ["Soup"]

    def test_context_units_default_accessible(self):
        import threading
        from unittest.mock import MagicMock

        ctx = PipelineContext(
            client=MagicMock(),
            semaphore=threading.Semaphore(1),
            units="book",
            category_tree=[],
            paprika_cats=[],
        )
        assert ctx.units == "book"
