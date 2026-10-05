"""The TAG stage (Cayenne Fix Roadmap F-246): one axis per call, and one axis's
failure costs only that axis. No model in sight; the categorise call is a fake."""
from __future__ import annotations

from typing import Dict, List

from recipeparser.core.stages.tag import TAG_BATCH_SIZE, TagFailure, tag_batch


class _Recipe:
    def __init__(self, title: str) -> None:
        self.title = title
        self.ingredient_lines = [f"{title} ingredient"]
        self.direction_steps = [f"Cook the {title}."]
        self.grid_categories: Dict[str, List[str]] = {"Stale": ["Old"]}
        self.categories: List[str] = ["Old"]


AXES = {"Cuisine": ["Italian", "Thai"], "Protein": ["Chicken", "Egg"]}


class _Answers:
    """Answers by axis; an axis in ``fail`` raises that many times before answering."""

    def __init__(self, by_axis: Dict[str, Dict[str, List[str]]], fail: Dict[str, int] = None) -> None:
        self.by_axis = by_axis
        self.fail = dict(fail or {})
        self.calls: List[tuple] = []

    def __call__(self, rows, axes):
        (axis,) = axes
        self.calls.append((axis, [r["id"] for r in rows]))
        if self.fail.get(axis, 0) > 0:
            self.fail[axis] -= 1
            raise RuntimeError(f"{axis} is down")
        return self.by_axis.get(axis, {})


def test_five_recipes_to_a_call():
    # Ten carried stock-based soups into Vegetarian beside vegetarian neighbours (2026-10-05).
    assert TAG_BATCH_SIZE == 5


def test_each_axis_is_asked_alone_over_every_recipe():
    answers = _Answers({})
    tag_batch([_Recipe("a"), _Recipe("b")], AXES, None, answers)
    assert answers.calls == [("Cuisine", ["recipe-1", "recipe-2"]), ("Protein", ["recipe-1", "recipe-2"])]


def test_the_prompt_reads_title_and_the_verbatim_lines():
    seen = []
    tag_batch([_Recipe("a")], {"Cuisine": ["Italian"]}, None, lambda rows, axes: seen.extend(rows) or {})
    assert seen == [{"id": "recipe-1", "title": "a", "ingredient_lines": ["a ingredient"],
                     "direction_steps": ["Cook the a."]}]


def test_the_answers_replace_whatever_the_recipe_carried():
    gnocchi, curry = _Recipe("gnocchi"), _Recipe("curry")
    answers = _Answers({
        "Cuisine": {"recipe-1": ["Italian"], "recipe-2": ["Thai"]},
        "Protein": {"recipe-2": ["Chicken"]},
    })
    assert tag_batch([gnocchi, curry], AXES, None, answers) == []
    assert gnocchi.grid_categories == {"Cuisine": ["Italian"]}
    assert gnocchi.categories == ["Italian"]
    assert curry.grid_categories == {"Cuisine": ["Thai"], "Protein": ["Chicken"]}
    assert curry.categories == ["Thai", "Chicken"]


def test_a_tag_not_offered_on_that_axis_is_not_written():
    recipe = _Recipe("gnocchi")
    tag_batch([recipe], AXES, None, _Answers({"Cuisine": {"recipe-1": ["Italian", "Egg", "Pasta"]}}))
    assert recipe.grid_categories == {"Cuisine": ["Italian"]}


def test_a_second_try_rescues_an_axis():
    recipe = _Recipe("curry")
    answers = _Answers({"Protein": {"recipe-1": ["Chicken"]}}, fail={"Protein": 1})
    assert tag_batch([recipe], AXES, None, answers) == []
    assert recipe.grid_categories == {"Protein": ["Chicken"]}
    assert [a for a, _ in answers.calls] == ["Cuisine", "Protein", "Protein"]


def test_an_axis_that_fails_twice_costs_only_that_axis():
    a, b = _Recipe("a"), _Recipe("b")
    answers = _Answers({"Protein": {"recipe-1": ["Egg"]}}, fail={"Cuisine": 2})
    failures = tag_batch([a, b], AXES, None, answers)
    assert failures == [TagFailure("Cuisine", ["a", "b"], "Cuisine is down")]
    assert a.grid_categories == {"Protein": ["Egg"]}
    assert b.grid_categories == {}
    assert [x for x, _ in answers.calls] == ["Cuisine", "Cuisine", "Protein"]


def test_an_axis_with_no_tags_is_not_asked():
    answers = _Answers({})
    tag_batch([_Recipe("a")], {"Empty": [], "Cuisine": ["Italian"]}, None, answers)
    assert [x for x, _ in answers.calls] == ["Cuisine"]
