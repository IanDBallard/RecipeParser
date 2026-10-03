"""What a tag may say about a recipe (Cayenne Fix Roadmap F-205).

The model tagged potato gnocchi Egg for the two eggs binding its dough, and
minestrone Chicken for its stock: both prompts asked only for tags that
"describe" or "apply to" a recipe. These tests hold the parts of the fix that
code can hold — one shared rules block in both prompts, the tree shown to the
model, no tag beside its own descendant, at most two tags an axis, no axis row
offered as a tag. Whether the model follows the rules is the golden set's job
(tests/goldens/test_tagging_golden.py).
"""
from __future__ import annotations

from typing import Dict, List

from recipeparser import gemini
from recipeparser.adapters.recat_worker import resolve_new_axes
from recipeparser.core.stages.categorize import (
    MAX_TAGS_PER_AXIS,
    axis_tags,
    categorize,
    filter_batch_result,
)
from recipeparser.models import CayenneRefinement

ROWS = [
    {"id": "c", "name": "Cuisine", "parent_id": None},
    {"id": "as", "name": "Asian", "parent_id": "c"},
    {"id": "th", "name": "Thai", "parent_id": "as"},
    {"id": "it", "name": "Italian", "parent_id": "c"},
    {"id": "p", "name": "Protein", "parent_id": None},
    {"id": "eg", "name": "Egg", "parent_id": "p"},
    {"id": "ch", "name": "Chicken", "parent_id": "p"},
    {"id": "q", "name": "Quick", "parent_id": None},
]
AXES: Dict[str, List[str]] = {"Cuisine": ["Asian", "Italian", "Thai"], "Protein": ["Chicken", "Egg"]}
PARENTS = {"Thai": "Asian"}


def _refined(grid: Dict[str, List[str]]) -> CayenneRefinement:
    return CayenneRefinement(
        title="T", base_servings=2, structured_ingredients=[], tokenized_directions=[],
        grid_categories=grid,
    )


class TestAxisTags:
    def test_offered_only_and_deduplicated(self):
        assert axis_tags(["Italian", "Nope", "Italian"], AXES["Cuisine"]) == ["Italian"]

    def test_at_most_two(self):
        assert MAX_TAGS_PER_AXIS == 2
        assert axis_tags(["Italian", "Asian", "Thai"], AXES["Cuisine"]) == ["Italian", "Asian", "Thai"][:2]

    def test_a_parent_is_pruned_before_the_cap(self):
        # Pruning first: Asian beside Thai must not take Italian's place.
        assert axis_tags(["Asian", "Thai", "Italian"], AXES["Cuisine"], PARENTS) == ["Thai", "Italian"]


class TestCategorize:
    def test_a_parent_beside_its_child_is_not_written(self):
        out = categorize(_refined({"Cuisine": ["Asian", "Thai"]}), AXES, PARENTS)
        assert out == {"Cuisine": ["Thai"]}

    def test_without_parents_nothing_is_pruned(self):
        out = categorize(_refined({"Cuisine": ["Asian", "Thai"]}), AXES)
        assert out == {"Cuisine": ["Asian", "Thai"]}

    def test_a_third_tag_on_an_axis_is_not_written(self):
        out = categorize(_refined({"Cuisine": ["Italian", "Asian", "Thai"]}), AXES)
        assert out == {"Cuisine": ["Italian", "Asian"]}


class TestFilterBatchResult:
    def test_with_axes_each_axis_is_held_like_an_ingest(self):
        offered = {"Asian", "Italian", "Thai", "Chicken", "Egg"}
        raw = {"r1": ["Asian", "Thai", "Italian", "Egg", "Chicken"], "r2": ["Asian"]}
        assert filter_batch_result(raw, offered, AXES, PARENTS) == {
            "r1": ["Thai", "Italian", "Egg", "Chicken"],
            "r2": ["Asian"],
        }

    def test_without_axes_it_is_unchanged(self):
        assert filter_batch_result({"r1": ["Asian", "Thai"]}, {"Asian", "Thai"}) == {"r1": ["Asian", "Thai"]}


class TestResolveNewAxes:
    def test_an_axis_with_children_is_not_offered(self):
        # A whole-axis job: the endpoint expands Cuisine to itself and its subtree.
        axes, ids = resolve_new_axes(ROWS, ["c", "as", "th", "it"])
        assert axes == {"Cuisine": ["Asian", "Thai", "Italian"]}
        assert "Cuisine" not in ids

    def test_a_childless_axis_is_still_its_own_tag(self):
        # A flat taxonomy is all axes; each must stay matchable.
        axes, ids = resolve_new_axes(ROWS, ["q"])
        assert axes == {"Quick": ["Quick"]} and ids == {"Quick": "q"}


class TestThePrompts:
    def test_both_prompts_carry_the_same_rules(self):
        refine = gemini.build_refine_prompt("RAW", None, AXES, PARENTS)
        batch = gemini.build_categorize_batch_prompt(
            [{"id": "r1", "title": "Gnocchi", "ingredient_lines": ["2 eggs"], "direction_steps": ["Mix."]}],
            AXES, PARENTS,
        )
        for line in gemini.TAGGING_RULES.splitlines():
            assert line in refine
            assert line in batch

    def test_the_rules_name_no_axis(self):
        # The axes are the cook's own; one rule serves every one of them.
        for axis in ("Protein", "Main Ingredient", "Cuisine", "Course", "Technique", "Diet"):
            assert axis not in gemini.TAGGING_RULES

    def test_a_nested_tag_is_shown_under_its_parent(self):
        refine = gemini.build_refine_prompt("RAW", None, AXES, PARENTS)
        assert '- Cuisine: ["Asian", "Italian", "Thai" (under "Asian")]' in refine
        batch = gemini.build_categorize_batch_prompt([], AXES, PARENTS)
        assert '- Cuisine: "Asian", "Italian", "Thai" (under "Asian")' in batch

    def test_without_parents_every_tag_is_flat(self):
        assert '- Cuisine: ["Asian", "Italian", "Thai"]' in gemini.build_refine_prompt("RAW", None, AXES)

    def test_the_batch_recipes_follow_their_marker(self):
        # The golden client keys a categorize reply by what follows RECIPES:.
        batch = gemini.build_categorize_batch_prompt(
            [{"id": "r1", "title": "Gnocchi", "ingredient_lines": [], "direction_steps": []}], AXES,
        )
        assert batch.index("RECIPES:") < batch.index("RECIPE ID: r1")
        assert batch.index("TAGGING RULES:") < batch.index("RECIPES:")
