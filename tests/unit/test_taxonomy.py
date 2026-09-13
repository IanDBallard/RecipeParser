"""core.taxonomy: the one walk from a category to its axis.

This module exists because ingest and bulk recategorise described the same
taxonomy differently until 2026-09-13 — `_build_axes` flattened every
descendant into its ROOT's axis, `resolve_new_axes` read the immediate parent —
and nothing tested a tree deeper than two levels, which is the only shape where
the two answers differ.
"""
from __future__ import annotations

import pytest

from recipeparser.core.taxonomy import axes_from_rows, descendants_of, root_of

THREE_DEEP = [
    {"id": "A", "name": "Cuisine", "parent_id": None},
    {"id": "F", "name": "Asian", "parent_id": "A"},
    {"id": "L", "name": "Thai", "parent_id": "F"},
    {"id": "Q", "name": "Quick", "parent_id": None},
]


class TestRootOf:
    def test_a_root_is_its_own_root(self):
        assert root_of("A", THREE_DEEP) == "A"

    def test_a_child_resolves_to_its_root(self):
        assert root_of("F", THREE_DEEP) == "A"

    def test_a_grandchild_resolves_to_the_same_root(self):
        # The case the old code got wrong: it would have answered "F".
        assert root_of("L", THREE_DEEP) == "A"

    def test_an_unknown_id_is_none(self):
        assert root_of("nope", THREE_DEEP) is None

    def test_a_parent_that_is_not_a_row_stops_the_walk(self):
        # An orphan: parent_id points at a row that is gone. The client displays
        # such a node at the root, so treating it as its own root agrees with
        # what the user is looking at.
        rows = [{"id": "x", "name": "Stray", "parent_id": "vanished"}]
        assert root_of("x", rows) == "x"

    def test_a_cycle_terminates(self):
        # Only reachable through a cross-device race, but it must not hang.
        rows = [{"id": "a", "name": "A", "parent_id": "b"},
                {"id": "b", "name": "B", "parent_id": "a"}]
        assert root_of("a", rows) in {"a", "b"}


class TestAxesFromRows:
    def test_every_descendant_flattens_into_its_root(self):
        assert axes_from_rows(THREE_DEEP) == {"Cuisine": ["Asian", "Thai"], "Quick": ["Quick"]}

    def test_a_childless_root_is_a_single_tag_axis(self):
        assert axes_from_rows([{"id": "Q", "name": "Quick", "parent_id": None}]) == {"Quick": ["Quick"]}

    def test_rows_without_a_name_or_id_are_skipped(self):
        rows = THREE_DEEP + [{"id": "", "name": "No id", "parent_id": None},
                             {"id": "z", "name": "  ", "parent_id": None}]
        assert axes_from_rows(rows) == {"Cuisine": ["Asian", "Thai"], "Quick": ["Quick"]}

    def test_tags_are_sorted(self):
        rows = [{"id": "A", "name": "Cuisine", "parent_id": None},
                {"id": "z", "name": "Zebra", "parent_id": "A"},
                {"id": "a", "name": "Apple", "parent_id": "A"}]
        assert axes_from_rows(rows)["Cuisine"] == ["Apple", "Zebra"]


class TestDescendantsOf:
    def test_a_root_offers_its_whole_subtree(self):
        assert descendants_of("A", THREE_DEEP) == ["A", "F", "L"]

    def test_a_mid_level_folder_offers_only_what_is_beneath_it(self):
        assert descendants_of("F", THREE_DEEP) == ["F", "L"]

    def test_a_leaf_offers_itself(self):
        assert descendants_of("L", THREE_DEEP) == ["L"]

    def test_an_unknown_id_offers_nothing(self):
        # This is what lets the endpoint tell "not yours" from "has no children".
        assert descendants_of("nope", THREE_DEEP) == []


def test_build_axes_delegates_and_is_unchanged():
    """SupabaseCategorySource._build_axes had no direct test before 2026-09-13.

    It is the ingest half of the pair that disagreed, so it needs one on the
    same fixture: if the delegation ever changes the flattening, the two paths
    diverge again and only this catches it.
    """
    from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource

    source = SupabaseCategorySource.__new__(SupabaseCategorySource)
    assert source._build_axes(THREE_DEEP) == axes_from_rows(THREE_DEEP)
    assert source._build_axes(THREE_DEEP) == {"Cuisine": ["Asian", "Thai"], "Quick": ["Quick"]}
