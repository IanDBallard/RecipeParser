"""The F-205 re-tag's decisions, with no model, network or database in sight."""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from scripts.retag_axis import (
    Change,
    apply_plan,
    axis_offer,
    created_after,
    main,
    parse_since,
    plan_retag,
    read_plan,
    write_plan,
)

ROWS = [
    {"id": "p", "name": "Protein", "parent_id": None},
    {"id": "eg", "name": "Egg", "parent_id": "p"},
    {"id": "ch", "name": "Chicken", "parent_id": "p"},
    {"id": "po", "name": "Poultry", "parent_id": "p"},
    {"id": "du", "name": "Duck", "parent_id": "po"},
    {"id": "c", "name": "Cuisine", "parent_id": None},
    {"id": "it", "name": "Italian", "parent_id": "c"},
]
GNOCCHI = {"id": "g", "title": "Potato Gnocchi", "ingredient_lines": [], "direction_steps": []}
CURRY = {"id": "k", "title": "Chicken Curry", "ingredient_lines": [], "direction_steps": []}


def _links(*pairs):
    return [{"recipe_id": r, "category_id": c} for r, c in pairs]


def _answer(by_recipe: Dict[str, List[str]]):
    def categorize(batch):
        return {r["id"]: by_recipe.get(r["id"], []) for r in batch}

    return categorize


def _changes(plan):
    return sorted((c.action, c.recipe_id, c.tag) for c in plan.changes)


class TestAxisOffer:
    def test_offers_the_axis_tags_but_not_the_axis_row(self):
        offer = axis_offer(ROWS, "Protein")
        assert offer.axes == {"Protein": ["Egg", "Chicken", "Poultry", "Duck"]}
        assert "Protein" not in offer.tag_ids
        assert offer.scope == {"p", "eg", "ch", "po", "du"}  # the axis row is in scope, to be dropped
        assert offer.parents["Duck"] == "Poultry"

    def test_an_unknown_or_ambiguous_axis_is_refused(self):
        with pytest.raises(ValueError, match="found 0"):
            axis_offer(ROWS, "Main Ingredient")
        with pytest.raises(ValueError, match="found 2"):
            axis_offer(ROWS + [{"id": "p2", "name": "Protein", "parent_id": None}], "Protein")

    def test_a_tag_named_like_the_axis_is_not_the_axis(self):
        with pytest.raises(ValueError, match="found 0"):
            axis_offer(ROWS, "Egg")


class TestPlanRetag:
    def test_the_answer_replaces_the_axis_links(self):
        offer = axis_offer(ROWS, "Protein")
        links = _links(("g", "eg"), ("k", "ch"), ("k", "eg"))
        plan = plan_retag([GNOCCHI, CURRY], links, offer, _answer({"k": ["Chicken"]}))
        assert _changes(plan) == [("drop", "g", "Egg"), ("drop", "k", "Egg")]
        assert plan.kept == 1

    def test_a_new_tag_is_added(self):
        offer = axis_offer(ROWS, "Protein")
        plan = plan_retag([CURRY], [], offer, _answer({"k": ["Chicken"]}))
        assert _changes(plan) == [("add", "k", "Chicken")]
        assert plan.changes[0].category_id == "ch"

    def test_links_on_other_axes_are_never_touched(self):
        offer = axis_offer(ROWS, "Protein")
        plan = plan_retag([GNOCCHI], _links(("g", "it")), offer, _answer({}))
        assert plan.changes == [] and plan.kept == 0

    def test_a_link_to_the_axis_row_is_dropped(self):
        offer = axis_offer(ROWS, "Protein")
        plan = plan_retag([CURRY], _links(("k", "p")), offer, _answer({"k": ["Chicken"]}))
        assert _changes(plan) == [("add", "k", "Chicken"), ("drop", "k", "Protein")]

    def test_a_parent_beside_its_child_is_not_added(self):
        offer = axis_offer(ROWS, "Protein")
        plan = plan_retag([CURRY], [], offer, _answer({"k": ["Poultry", "Duck"]}))
        assert _changes(plan) == [("add", "k", "Duck")]

    def test_a_batch_that_fails_twice_keeps_every_link(self):
        offer = axis_offer(ROWS, "Protein")

        def broken(batch):
            raise RuntimeError("model down")

        plan = plan_retag([GNOCCHI], _links(("g", "eg")), offer, broken)
        assert plan.changes == []
        assert plan.failed == [("Potato Gnocchi", "model down")]

    def test_a_batch_that_fails_once_is_retried(self):
        offer = axis_offer(ROWS, "Protein")
        calls: List[int] = []

        def flaky(batch):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("blip")
            return {}

        plan = plan_retag([GNOCCHI], _links(("g", "eg")), offer, flaky)
        assert len(calls) == 2 and plan.failed == []
        assert _changes(plan) == [("drop", "g", "Egg")]

    def test_recipes_go_in_batches(self):
        offer = axis_offer(ROWS, "Protein")
        sizes: List[int] = []

        def count(batch):
            sizes.append(len(batch))
            return {}

        recipes = [{"id": f"r{i}", "title": "", "ingredient_lines": [], "direction_steps": []} for i in range(25)]
        plan_retag(recipes, [], offer, count)
        assert sizes == [10, 10, 5]


class TestThePlanFile:
    def test_round_trip(self, tmp_path):
        offer = axis_offer(ROWS, "Protein")
        plan = plan_retag([GNOCCHI, CURRY], _links(("g", "eg")), offer, _answer({"k": ["Chicken"]}))
        path = str(tmp_path / "plan.csv")
        write_plan(path, "u1", plan)
        assert read_plan(path, "u1") == plan.changes

    def test_a_plan_for_another_user_is_refused(self, tmp_path):
        path = str(tmp_path / "plan.csv")
        offer = axis_offer(ROWS, "Protein")
        write_plan(path, "u1", plan_retag([GNOCCHI], _links(("g", "eg")), offer, _answer({})))
        with pytest.raises(ValueError, match="not u2"):
            read_plan(path, "u2")


class _Recorder:
    """Records each write as (op, recipe_id, category_id); fails the ones named."""

    def __init__(self, fail_on=()):
        self.writes: List[tuple] = []
        self.fail_on = set(fail_on)

    def table(self, name):
        assert name == "recipe_categories"
        return _Query(self)


class _Query:
    def __init__(self, owner):
        self.owner, self.op, self.row, self.filters = owner, None, None, {}

    def upsert(self, rows, **kw: Any):
        assert kw == {"on_conflict": "recipe_id,category_id", "ignore_duplicates": True}
        self.op, self.row = "add", rows[0]
        return self

    def delete(self):
        self.op = "drop"
        return self

    def eq(self, col, value):
        self.filters[col] = value
        return self

    def execute(self):
        rid = self.row["recipe_id"] if self.op == "add" else self.filters["recipe_id"]
        cid = self.row["category_id"] if self.op == "add" else self.filters["category_id"]
        if (self.op, rid, cid) in self.owner.fail_on:
            raise RuntimeError("rejected")
        if self.op == "add":
            assert self.row["user_id"] == "u1"
        self.owner.writes.append((self.op, rid, cid))


def test_apply_adds_before_it_drops():
    changes = [Change("drop", "k", "Curry", "Egg", "eg"), Change("add", "k", "Curry", "Chicken", "ch")]
    sb = _Recorder()
    assert apply_plan(sb, "u1", changes) == (1, 1, 0)
    assert sb.writes == [("add", "k", "ch"), ("drop", "k", "eg")]


def test_apply_reports_a_failed_write_and_carries_on():
    changes = [Change("drop", "g", "Gnocchi", "Egg", "eg"), Change("drop", "k", "Curry", "Egg", "eg")]
    sb = _Recorder(fail_on={("drop", "g", "eg")})
    assert apply_plan(sb, "u1", changes) == (0, 1, 1)
    assert sb.writes == [("drop", "k", "eg")]


class TestSince:
    """--since (Cayenne Fix Roadmap F-246, design D3): re-tag only what was imported after a cutoff."""

    def test_a_zoned_timestamp_is_read_as_utc(self):
        assert parse_since("2026-10-03T21:00:00Z").isoformat() == "2026-10-03T21:00:00+00:00"
        assert parse_since("2026-10-04T07:00:00+10:00").isoformat() == "2026-10-03T21:00:00+00:00"

    def test_a_naive_or_garbled_timestamp_is_refused(self):
        with pytest.raises(ValueError, match="no time zone"):
            parse_since("2026-10-03T21:00:00")
        with pytest.raises(ValueError, match="not an ISO 8601"):
            parse_since("last Friday")

    def test_only_recipes_created_after_the_cutoff_are_kept(self):
        rows = [
            {"id": "before", "created_at": "2026-10-03T20:59:59+00:00"},
            {"id": "at", "created_at": "2026-10-03T21:00:00+00:00"},
            {"id": "after", "created_at": "2026-10-03T21:00:01.123456+00:00"},
            {"id": "zulu", "created_at": "2026-10-05T08:00:00Z"},
            {"id": "none", "created_at": None},
        ]
        kept = created_after(rows, parse_since("2026-10-03T21:00:00Z"))
        assert [r["id"] for r in kept] == ["after", "zulu"]

    def test_since_only_narrows_a_plan(self):
        with pytest.raises(SystemExit, match="only narrows a --plan"):
            main(["--user-id", "u", "--apply", "x.csv", "--since", "2026-10-03T21:00:00Z"])

    def test_a_bad_since_stops_before_any_connection(self, monkeypatch):
        monkeypatch.setattr("scripts.retag_axis._supabase", lambda: pytest.fail("connected before checking --since"))
        with pytest.raises(SystemExit, match="no time zone"):
            main(["--user-id", "u", "--axis", "Protein", "--plan", "p.csv", "--since", "2026-10-03"])
