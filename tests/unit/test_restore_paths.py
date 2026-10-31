"""Categories created on restore: level by level, reusing names, capped at depth 4."""
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.io.category_sources import restore_paths as rp

U = "user-1"


def _resp(data=None):
    r = MagicMock()
    r.json.return_value = data if data is not None else []
    r.raise_for_status.return_value = None
    return r


class _Db:
    """A tiny fake: GET returns the current rows, POST adds them (unless a name is raced)."""

    def __init__(self, rows=(), swallow=(), raced=()):
        self.rows = [dict(r) for r in rows]
        self.swallow = set(swallow)  # POSTed names the conflict ignores
        self.raced = list(raced)     # rows another job inserts when the POST lands
        self.posts = []

    def get(self, url, **kw):
        assert kw["params"]["user_id"] == f"eq.{U}"
        return _resp([dict(r) for r in self.rows])

    def post(self, url, **kw):
        assert kw["params"] == {"on_conflict": "user_id,name"}
        assert kw["headers"]["Prefer"] == "resolution=ignore-duplicates,return=minimal"
        self.posts.append(kw["json"])
        for row in kw["json"]:
            if row["name"] not in self.swallow:
                self.rows.append({"id": row["id"], "name": row["name"], "parent_id": row["parent_id"]})
        self.rows.extend(self.raced)
        self.raced = []
        return _resp()


def _run(db, paths):
    with patch.object(rp.httpx, "get", side_effect=db.get), patch.object(rp.httpx, "post", side_effect=db.post), \
         patch.object(rp, "live_writes_blocked", return_value=False):
        return rp.ensure_category_paths(U, paths, supabase_url="https://x", service_key="k")


def test_creates_missing_path_level_by_level():
    db = _Db()
    out = _run(db, [["Course", "Dessert", "Cake"]])
    assert [[r["name"] for r in p] for p in db.posts] == [["Course"], ["Dessert"], ["Cake"]]
    ids = {r["name"]: r["id"] for r in db.rows}
    assert db.posts[0][0]["parent_id"] is None
    assert db.posts[1][0]["parent_id"] == ids["Course"]
    assert db.posts[2][0]["parent_id"] == ids["Dessert"]
    assert db.posts[2][0]["user_id"] == U
    assert out == {("Course", "Dessert", "Cake"): "Cake"}


def test_reuses_existing_name_anywhere():
    db = _Db(rows=[{"id": "m", "name": "Meal", "parent_id": None}, {"id": "d", "name": "Dessert", "parent_id": "m"}])
    out = _run(db, [["Course", "Dessert", "Cake"]])
    created = [r["name"] for p in db.posts for r in p]
    assert "Dessert" not in created and created == ["Course", "Cake"]
    assert db.posts[1][0]["parent_id"] == "d"
    assert out[("Course", "Dessert", "Cake")] == "Cake"


def test_concurrent_insert_is_ignored():
    db = _Db(rows=[{"id": "c", "name": "Course", "parent_id": None}, {"id": "d", "name": "Dessert", "parent_id": "c"}],
             swallow={"Cake"}, raced=[{"id": "k", "name": "Cake", "parent_id": "d"}])
    out = _run(db, [["Course", "Dessert", "Cake"]])
    assert out == {("Course", "Dessert", "Cake"): "Cake"}


def test_depth_limit_stops_at_deepest_legal_node():
    db = _Db(rows=[
        {"id": "1", "name": "A", "parent_id": None}, {"id": "2", "name": "B", "parent_id": "1"},
        {"id": "3", "name": "C", "parent_id": "2"}, {"id": "4", "name": "X", "parent_id": "3"},
    ])
    out = _run(db, [["X", "Y"]])
    assert db.posts == []
    assert out == {("X", "Y"): "X"}


def test_refuses_in_a_test_run():
    with pytest.raises(RuntimeError):
        rp.ensure_category_paths(U, [["A"]], supabase_url="https://x", service_key="k")


def test_restore_grid_is_sorted_and_deduplicated():
    assert rp.restore_grid(["b", "a", "b"]) == {"restore": ["a", "b"]}


def test_a_five_long_new_path_creates_four_nodes_and_places_the_fourth():
    db = _Db()
    out = _run(db, [["A", "B", "C", "D", "E"]])
    assert [r["name"] for p in db.posts for r in p] == ["A", "B", "C", "D"]
    assert out == {("A", "B", "C", "D", "E"): "D"}


def test_padded_names_are_cleaned_the_same_way_everywhere():
    assert rp.clean_path([" Course ", "", "  ", "Cake	"]) == ("Course", "Cake")
    db = _Db()
    out = _run(db, [[" Course ", "Cake "]])
    assert out == {("Course", "Cake"): "Cake"}


class _ListsDb(_Db):
    """_Db whose GET honours its select: is_lists_root is only returned when asked for."""

    def get(self, url, **kw):
        cols = kw["params"]["select"].split(",")
        return _resp([{c: r.get(c) for c in cols} for r in self.rows])


def _run_lists(db, paths, list_paths):
    with patch.object(rp.httpx, "get", side_effect=db.get), patch.object(rp.httpx, "post", side_effect=db.post),          patch.object(rp, "live_writes_blocked", return_value=False):
        return rp.ensure_category_paths(
            U, paths, list_paths=list_paths, supabase_url="https://x", service_key="k",
        )


MY_LISTS = {"id": "L", "name": "My lists", "parent_id": None, "is_lists_root": True}


def test_list_paths_land_under_the_lists_root():
    db = _ListsDb(rows=[MY_LISTS])
    out = _run_lists(db, [], [["For Sue"], ["Parties", "Birthday"]])
    parents = {r["name"]: r["parent_id"] for p in db.posts for r in p}
    ids = {r["name"]: r["id"] for r in db.rows}
    assert parents == {"For Sue": "L", "Parties": "L", "Birthday": ids["Parties"]}
    assert "Lists" not in {r["name"] for r in db.rows}
    assert out[("For Sue",)] == "For Sue"
    assert out[("Parties", "Birthday")] == "Birthday"


def test_list_path_reuses_existing_pill():
    db = _ListsDb(rows=[MY_LISTS, {"id": "t", "name": "To try", "parent_id": "L"}])
    out = _run_lists(db, [], [["To try"]])
    assert db.posts == []
    assert out == {("To try",): "To try"}


def test_list_path_reuses_a_name_elsewhere_in_the_tree():
    db = _ListsDb(rows=[MY_LISTS, {"id": "c", "name": "Soup", "parent_id": None}])
    out = _run_lists(db, [], [["Soup"]])
    assert db.posts == []
    assert out == {("Soup",): "Soup"}


def test_list_paths_without_a_lists_root_are_skipped_and_logged(caplog):
    db = _ListsDb(rows=[{"id": "c", "name": "Soup", "parent_id": None}])
    out = _run_lists(db, [["Soup"]], [["For Sue"]])
    assert db.posts == []
    assert out == {("Soup",): "Soup"}
    assert "Lists root" in caplog.text


def test_categories_and_list_paths_restore_together():
    db = _ListsDb(rows=[MY_LISTS])
    out = _run_lists(db, [["Course", "Cake"]], [["For Sue"]])
    assert out == {("Course", "Cake"): "Cake", ("For Sue",): "For Sue"}
    assert {r["name"] for p in db.posts for r in p} == {"Course", "Cake", "For Sue"}
