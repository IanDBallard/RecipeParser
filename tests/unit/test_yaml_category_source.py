"""Unit tests for YamlCategorySource: both the ``axes:`` format and the
``categories:`` tree that the app itself writes (issue #149)."""
from __future__ import annotations

import pytest

from recipeparser.io.category_sources.yaml_source import (
    CATEGORIES_AXIS,
    YamlCategorySource,
)
from recipeparser.paths import get_bundled_categories_file


def _source(tmp_path, text: str) -> YamlCategorySource:
    path = tmp_path / "categories.yaml"
    path.write_text(text, encoding="utf-8")
    return YamlCategorySource(path)


class TestAxesFormat:

    def test_axes_load_as_written(self, tmp_path):
        src = _source(tmp_path, "axes:\n  Cuisine: [Italian, Thai]\n  Course: [Dessert]\n")
        assert src.load_axes() == {"Cuisine": ["Italian", "Thai"], "Course": ["Dessert"]}

    def test_axes_format_has_no_parents(self, tmp_path):
        src = _source(tmp_path, "axes:\n  Cuisine: [Italian]\n")
        assert src.load_parents() == {}


class TestCategoriesTreeFormat:

    TREE = (
        "categories:\n"
        "- Breakfast\n"
        "- Dessert:\n"
        "  - Cake\n"
        "  - Pie\n"
        "- 'Grilled '\n"
        "- Regional Cuisine:\n"
        "  - Indian\n"
    )

    def test_every_category_is_offered_on_one_axis(self, tmp_path):
        """One axis, so TAG makes one call per batch - not one per top-level name,
        which would be dozens of calls on a 5-RPM free key."""
        axes = _source(tmp_path, self.TREE).load_axes()
        assert axes == {
            CATEGORIES_AXIS: ["Breakfast", "Dessert", "Cake", "Pie", "Grilled", "Regional Cuisine", "Indian"],
        }

    def test_children_keep_their_parent(self, tmp_path):
        assert _source(tmp_path, self.TREE).load_parents() == {
            "Cake": "Dessert",
            "Pie": "Dessert",
            "Indian": "Regional Cuisine",
        }

    def test_a_parent_with_no_children_is_a_plain_category(self, tmp_path):
        src = _source(tmp_path, "categories:\n- Soup:\n- Salads\n")
        assert src.load_axes() == {CATEGORIES_AXIS: ["Soup", "Salads"]}
        assert src.load_parents() == {}

    def test_duplicate_names_offered_once(self, tmp_path):
        src = _source(tmp_path, "categories:\n- Grilled\n- Mains:\n  - Grilled\n")
        assert src.load_axes() == {CATEGORIES_AXIS: ["Grilled", "Mains"]}
        assert src.load_parents() == {}  # first seen at the top level

    def test_empty_tree_offers_nothing(self, tmp_path):
        assert _source(tmp_path, "categories: []\n").load_axes() == {}

    def test_bundled_default_loads(self):
        """The file shipped with the app (and copied to every new user's AppData)
        must give the pipeline something to tag with."""
        src = YamlCategorySource(get_bundled_categories_file())
        axes = src.load_axes()
        assert "Indian" in axes[CATEGORIES_AXIS]
        assert src.load_parents()["Indian"] == "Regional Cuisine"


class TestUnusableFiles:

    @pytest.mark.parametrize("text", ["", "just a string\n", "other: 1\n", "axes: [a, b]\n"])
    def test_unusable_shapes_offer_nothing(self, tmp_path, text):
        src = _source(tmp_path, text)
        assert src.load_axes() == {}
        assert src.load_parents() == {}

    def test_missing_file_offers_nothing(self, tmp_path):
        src = YamlCategorySource(tmp_path / "nope.yaml")
        assert src.load_axes() == {}
        assert src.load_parents() == {}
