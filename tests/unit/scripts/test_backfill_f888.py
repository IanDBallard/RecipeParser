"""scripts/backfill_f888.py — stored recipes get the parser fixes new imports have (Cayenne F-888)."""
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "backfill_f888", Path(__file__).parents[3] / "scripts" / "backfill_f888.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["backfill_f888"] = mod
_spec.loader.exec_module(mod)

RowChange = mod.RowChange
CHILLI = "https://www.bbcgoodfood.com/recipes/chilli"


# ── sources (F-878) ────────────────────────────────────────────────────────────

def test_sources_replaces_an_address_title_with_the_pages_host():
    [c] = mod.plan_sources([{"id": "a", "source_title": CHILLI, "source_url": CHILLI}])
    assert c.recipe_id == "a"
    assert c.new == {"source_title": "bbcgoodfood.com"}
    assert c.old == {"source_title": CHILLI}


def test_sources_falls_back_to_the_titles_host_without_a_url():
    [c] = mod.plan_sources([{"id": "b", "source_title": "http://example.org/x", "source_url": None}])
    assert c.new == {"source_title": "example.org"}


@pytest.mark.parametrize("title", ["Serious Eats", "seriouseats.com", None, ""])
def test_sources_leaves_names_and_bare_hosts(title):
    assert mod.plan_sources([{"id": "c", "source_title": title, "source_url": CHILLI}]) == []


# ── SQL ────────────────────────────────────────────────────────────────────────

def _title_change():
    return RowChange("11111111-1111-1111-1111-111111111111", {"source_title": CHILLI},
                     {"source_title": "bbcgoodfood.com"}, "title")


def test_render_sql_guards_each_written_column_and_ends_in_rollback():
    sql = mod.render_sql([_title_change()], label="sources", commit=False)
    assert sql.startswith("begin;")
    assert f"source_title is not distinct from '{CHILLI}'" in sql
    assert "set source_title = 'bbcgoodfood.com'" in sql
    assert "'11111111-1111-1111-1111-111111111111'::uuid" in sql
    assert sql.endswith("rollback;\n")
    assert mod.render_sql([_title_change()], label="sources", commit=True).endswith("commit;\n")


def test_render_sql_ends_with_a_verification_row():
    sql = mod.render_sql([_title_change()], label="sources", commit=False)
    verification = sql[sql.rindex("select"):]
    assert "as changed" in verification and "1 as planned" in verification


def test_guard_names_only_written_columns():
    sql = mod.render_sql([_title_change()], label="sources", commit=False)
    update = sql[sql.index("update"):sql.index(";", sql.index("update"))]
    assert "structured_ingredients" not in update
    assert "cook_" not in update


def test_literals_round_trip_quotes_and_unicode():
    assert mod.sql_literal("Nigella's purée ½ \\ x", "text") == "'Nigella''s purée ½ \\ x'"
    assert mod.sql_literal(None, "text") == "null"
    assert mod.sql_literal(45, "integer") == "45"
    jsonb = mod.sql_literal([{"fallback_string": "Nigella's purée"}], "jsonb")
    assert jsonb.endswith("'::jsonb") and "Nigella''s purée" in jsonb


def test_a_null_old_value_is_guarded_with_is_not_distinct_from_null():
    c = RowChange("11111111-1111-1111-1111-111111111111", {"cook_time": None}, {"cook_time": "1 hour"}, "t")
    assert "cook_time is not distinct from null" in mod.render_sql([c], label="x", commit=False)


# ── record and restore ─────────────────────────────────────────────────────────

def test_invert_then_record_round_trips(tmp_path):
    changes = [_title_change(),
               RowChange("22222222-2222-2222-2222-222222222222",
                         {"structured_ingredients": [{"a": 1, "x": None}], "cook_min_minutes": None},
                         {"structured_ingredients": [{"a": 2, "x": None}], "cook_min_minutes": 30}, "n")]
    path = tmp_path / "rec.json"
    mod.write_record(changes, path)
    assert mod.read_record(path) == changes
    assert mod.invert(mod.invert(changes)) == changes
    assert mod.invert(changes)[0].new == {"source_title": CHILLI}


# ── CLI ────────────────────────────────────────────────────────────────────────

def test_commit_without_record_is_refused(tmp_path, monkeypatch):
    def no_client(*_a, **_k):
        raise AssertionError("no client may be built")
    monkeypatch.setattr(mod, "_client", no_client)
    with pytest.raises(SystemExit):
        mod.main(["sources", "--sql", str(tmp_path / "x.sql"), "--commit"])


def test_commit_without_sql_is_refused(monkeypatch):
    monkeypatch.setattr(mod, "_client", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(SystemExit):
        mod.main(["sources", "--commit"])


# ── sizes (F-858, F-892) ───────────────────────────────────────────────────────

def _item(line, amount, unit=None, **over):
    item = {"id": "ing_01", "kind": "ingredient", "amount": amount, "unit": unit, "name": "thing",
            "fallback_string": line, "converted_amount": None, "converted_unit": None,
            "is_ai_converted": False, "line_index": 0, "state": None}
    item.update(over)
    return item


def test_sizes_fills_the_safe_lines_only():
    items = [_item("1 standing rib roast (about 8 lb)", 1.0),
             _item("9 large egg whites (about 280-300 gms)", 9.0),
             _item("2 (15-ounce) cans white beans", 2.0, "cans"),
             {"id": "h", "kind": "heading", "amount": None, "unit": None, "name": "For the beans",
              "fallback_string": "For the beans (15 oz)"}]
    [c] = mod.plan_sizes([{"id": "r", "structured_ingredients": items}])
    new = c.new["structured_ingredients"]
    assert (new[0]["converted_amount"], new[0]["converted_unit"], new[0]["is_ai_converted"]) == (8.0, "lb", False)
    assert (new[2]["converted_amount"], new[2]["converted_unit"]) == (15.0, "ounce")
    assert new[1] == items[1] and new[3] == items[3]
    assert c.old == {"structured_ingredients": items}
    assert items[0]["converted_amount"] is None  # the input is not mutated


def test_unknown_item_keys_survive():
    item = _item("1 standing rib roast (about 8 lb)", 1.0, x_cayenne=1)
    del item["line_index"]
    [c] = mod.plan_sizes([{"id": "r", "structured_ingredients": [item]}])
    got = c.new["structured_ingredients"][0]
    assert got["x_cayenne"] == 1
    assert set(got) == set(item)


@pytest.mark.parametrize("item", [
    _item("1 roast (about 8 lb)", 1.0, "g"),
    _item("1 roast (about 8 lb)", 1.0, converted_amount=5.0, converted_unit="lb"),
    _item("1 roast (about 8 lb)", None),
])
def test_sizes_skips_a_weight_unit_a_second_measure_or_no_amount(item):
    assert mod.plan_sizes([{"id": "r", "structured_ingredients": [item]}]) == []


@pytest.mark.parametrize("ingredients", [None, {}, [], [_item(None, 1.0)], ["a string"]])
def test_non_list_ingredients_are_skipped(ingredients):
    assert mod.plan_sizes([{"id": "r", "structured_ingredients": ingredients}]) == []


# ── totals (F-877a) ────────────────────────────────────────────────────────────

def _times(**over):
    row = {"id": "t", "prep_time": "15 minutes", "cook_time": None,
           "prep_min_minutes": 15, "prep_max_minutes": 15, "prep_note": None,
           "cook_min_minutes": 60, "cook_max_minutes": 60, "cook_note": None}
    row.update(over)
    return row


def test_totals_takes_prep_off_a_cook_that_came_from_a_total():
    [c] = mod.plan_totals([_times()])
    assert c.new == {"cook_min_minutes": 45, "cook_max_minutes": 45}
    assert c.old == {"cook_min_minutes": 60, "cook_max_minutes": 60}


@pytest.mark.parametrize("over", [
    {"prep_min_minutes": 20, "prep_max_minutes": 20},   # prep edited since import
    {"cook_time": "1 hour"},                            # the page stated a cook time
    {"cook_max_minutes": 75},                           # a range
    {"cook_note": "about"},                             # a note
    {"cook_min_minutes": 15, "cook_max_minutes": 15},   # not longer than the prep
    {"prep_time": None},                                # not imported (typed by a cook)
    {"cook_min_minutes": None, "cook_max_minutes": None},
])
def test_totals_leaves_edited_prep_cook_text_ranges_and_notes(over):
    assert mod.plan_totals([_times(**over)]) == []


# ── idempotence ────────────────────────────────────────────────────────────────

def _apply(rows, changes):
    by_id = {c.recipe_id: c.new for c in changes}
    return [{**r, **by_id.get(r["id"], {})} for r in rows]


def test_a_second_run_plans_nothing():
    sizes = [{"id": "r", "structured_ingredients": [_item("1 standing rib roast (about 8 lb)", 1.0)]}]
    sources = [{"id": "s", "source_title": CHILLI, "source_url": CHILLI}]
    totals = [_times()]
    for planner, rows in ((mod.plan_sizes, sizes), (mod.plan_sources, sources)):
        first = planner(rows)
        assert first
        assert planner(_apply(rows, first)) == []
    # A repaired total still looks like one (cook > prep, no cook text): the earlier record's ids are skipped.
    first = mod.plan_totals(totals)
    assert first
    assert mod.plan_totals(_apply(totals, first), skip_ids={c.recipe_id for c in first}) == []


def test_skip_record_ids_come_from_earlier_records(tmp_path):
    p = tmp_path / "totals.json"
    mod.write_record(mod.plan_totals([_times()]), p)
    assert mod.record_ids([p]) == {"t"}


def test_an_existing_record_is_never_overwritten(tmp_path, monkeypatch):
    p = tmp_path / "rec.json"
    p.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(mod, "_client", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(SystemExit):
        mod.main(["sources", "--record", str(p)])
