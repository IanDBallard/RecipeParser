"""Tests for recipeparser.categories — YAML taxonomy and categorisation."""
import json
import pytest
from unittest.mock import MagicMock

from tests.conftest import make_mock_client
from recipeparser.models import RecipeExtraction
from recipeparser.categories import (
    load_category_tree,
    build_paprika_categories,
    categorise_recipe,
)

# Load the real taxonomy once for tests that need PAPRIKA_CATEGORIES.
_CATEGORY_TREE = load_category_tree()
PAPRIKA_CATEGORIES = build_paprika_categories(_CATEGORY_TREE)


def _make_recipe(name: str, ingredients=None, notes=None) -> RecipeExtraction:
    return RecipeExtraction(
        name=name,
        ingredients=ingredients or ["flour", "water"],
        directions=["Mix and bake."],
        notes=notes,
    )


# ---------------------------------------------------------------------------
# load_category_tree  — YAML taxonomy loader
# ---------------------------------------------------------------------------

class TestLoadCategoryTree:

    def _write_yaml(self, tmp_path, content: str):
        p = tmp_path / "categories.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_loads_top_level_categories(self, tmp_path):
        p = self._write_yaml(tmp_path, "categories:\n  - Soup\n  - Salads\n")
        tree = load_category_tree(p)
        assert ("Soup", None) in tree
        assert ("Salads", None) in tree

    def test_loads_subcategories_with_parent(self, tmp_path):
        p = self._write_yaml(tmp_path, "categories:\n  - Dessert:\n      - Cake\n      - Pie\n")
        tree = load_category_tree(p)
        assert ("Dessert", None) in tree
        assert ("Cake", "Dessert") in tree
        assert ("Pie", "Dessert") in tree

    def test_mixed_top_level_and_nested(self, tmp_path):
        p = self._write_yaml(tmp_path, (
            "categories:\n"
            "  - Soup\n"
            "  - Mains:\n"
            "      - Beef Dishes\n"
            "  - Salads\n"
        ))
        tree = load_category_tree(p)
        assert ("Soup", None) in tree
        assert ("Mains", None) in tree
        assert ("Beef Dishes", "Mains") in tree
        assert ("Salads", None) in tree

    def test_missing_file_returns_empty_list(self, tmp_path):
        tree = load_category_tree(tmp_path / "nonexistent.yaml")
        assert tree == []

    def test_malformed_yaml_returns_empty_list(self, tmp_path):
        p = self._write_yaml(tmp_path, "categories: ][invalid yaml")
        tree = load_category_tree(p)
        assert tree == []

    def test_empty_categories_list_returns_empty(self, tmp_path):
        p = self._write_yaml(tmp_path, "categories: []\n")
        tree = load_category_tree(p)
        assert tree == []

    def test_paprika_categories_derived_from_tree(self):
        """PAPRIKA_CATEGORIES must only contain leaf names (no duplicates)."""
        assert len(PAPRIKA_CATEGORIES) == len(set(PAPRIKA_CATEGORIES))
        tree_leaves = {leaf for leaf, _ in _CATEGORY_TREE}
        for cat in PAPRIKA_CATEGORIES:
            assert cat in tree_leaves

    def test_real_categories_yaml_loads_correctly(self):
        """The actual categories.yaml in the project must parse without errors."""
        from recipeparser.categories import _CATEGORIES_FILE
        tree = load_category_tree(_CATEGORIES_FILE)
        assert len(tree) > 0, "categories.yaml loaded but was empty"
        leaves = {leaf for leaf, _ in tree}
        assert "Soup" in leaves
        assert "Cake" in leaves
        assert "Italian" in leaves
        assert "Chicken Dishes" in leaves


# ---------------------------------------------------------------------------
# categorise_recipe  (mocked — no live API calls)
# ---------------------------------------------------------------------------

class TestCategoriseRecipe:

    def _mock_response(self, categories: list) -> MagicMock:
        resp = MagicMock()
        resp.text = json.dumps(categories)
        return resp

    def test_valid_single_category_returned(self):
        client = make_mock_client(return_value=self._mock_response(["Pizza"]))
        result = categorise_recipe(
            _make_recipe("Margherita Pizza"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        assert result == ["Pizza"]

    def test_valid_multiple_categories_returned(self):
        client = make_mock_client(return_value=self._mock_response(["Cake", "Dessert"]))
        result = categorise_recipe(
            _make_recipe("Chocolate Cake"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        assert result == ["Cake", "Dessert"]

    def test_invalid_category_filtered_out(self):
        client = make_mock_client(
            return_value=self._mock_response(["Pizza", "Made Up Category", "Soup"])
        )
        result = categorise_recipe(
            _make_recipe("Pizza Soup"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        assert "Made Up Category" not in result
        assert set(result) == {"Pizza", "Soup"}

    def test_all_invalid_categories_falls_back(self):
        client = make_mock_client(
            return_value=self._mock_response(["Nonsense", "Also Nonsense"])
        )
        result = categorise_recipe(
            _make_recipe("Mystery Dish"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        assert result == ["EPUB Imports"]

    def test_api_exception_falls_back(self):
        client = make_mock_client(side_effect=Exception("network error"))
        result = categorise_recipe(
            _make_recipe("Pasta Carbonara"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        assert result == ["EPUB Imports"]

    def test_empty_list_response_falls_back(self):
        client = make_mock_client(return_value=self._mock_response([]))
        result = categorise_recipe(
            _make_recipe("Empty Recipe"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        assert result == ["EPUB Imports"]

    def test_markdown_fences_stripped(self):
        resp = MagicMock()
        resp.text = '```json\n["Soup"]\n```'
        client = make_mock_client(return_value=resp)
        result = categorise_recipe(
            _make_recipe("Tomato Soup"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        assert result == ["Soup"]

    def test_recipe_name_included_in_prompt(self):
        client = make_mock_client(return_value=self._mock_response(["Soup"]))
        categorise_recipe(
            _make_recipe("Pho Bo"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        assert "Pho Bo" in prompt

    def test_all_taxonomy_entries_in_prompt(self):
        client = make_mock_client(return_value=self._mock_response(["Soup"]))
        categorise_recipe(
            _make_recipe("Minestrone"), _CATEGORY_TREE, PAPRIKA_CATEGORIES, client
        )
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        for cat in PAPRIKA_CATEGORIES:
            assert cat in prompt, f"Category '{cat}' missing from prompt"
