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
CUTOFF = "2026-11-27T17:00:00+00:00"
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
    assert mod.sql_literal("Nigella's pie \\ x", "text") == "'Nigella''s pie \\ x'"
    assert mod.sql_literal("Nigella's purée ½ \\ x", "text") == r"U&'Nigella''s pur\00e9e \00bd \\ x'"
    assert mod.sql_literal(None, "text") == "null"
    assert mod.sql_literal(45, "integer") == "45"
    jsonb = mod.sql_literal([{"fallback_string": "Nigella's purée"}], "jsonb")
    assert jsonb.endswith("'::jsonb") and ("Nigella''s pur" + chr(92) + "u00e9e") in jsonb and jsonb.isascii()


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
    row = {"id": "t", "created_at": "2026-11-20T10:00:00+00:00", "prep_time": "15 minutes", "cook_time": None,
           "prep_min_minutes": 15, "prep_max_minutes": 15, "prep_note": None,
           "cook_min_minutes": 60, "cook_max_minutes": 60, "cook_note": None}
    row.update(over)
    return row


def test_totals_takes_prep_off_a_cook_that_came_from_a_total():
    [c] = mod.plan_totals([_times()], created_before=CUTOFF)
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
    assert mod.plan_totals([_times(**over)], created_before=CUTOFF) == []


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
    first = mod.plan_totals(totals, created_before=CUTOFF)
    assert first
    assert mod.plan_totals(_apply(totals, first), skip_ids={c.recipe_id for c in first}, created_before=CUTOFF) == []


def test_skip_record_ids_come_from_earlier_records(tmp_path):
    p = tmp_path / "totals.json"
    mod.write_record(mod.plan_totals([_times()], created_before=CUTOFF), p)
    assert mod.record_ids([p]) == {"t"}


def test_an_existing_record_is_never_overwritten(tmp_path, monkeypatch):
    p = tmp_path / "rec.json"
    p.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(mod, "_client", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(SystemExit):
        mod.main(["sources", "--record", str(p)])


# ── fetch-times (F-877, the page's JSON-LD times) ───────────────────────────────

from recipeparser.exceptions import SiteRefusedError, UrlFetchError  # noqa: E402


def _page(**times):
    body = ", ".join(f'"{k}": "{v}"' for k, v in times.items())
    recipe = '{"@type": "Recipe"' + (", " + body if body else "") + "}"
    return f'<html><head><script type="application/ld+json">{recipe}</script></head></html>'


def _web(rid="w", host="example.com", **over):
    row = {"id": rid, "source_url": f"https://{host}/r/{rid}", "source_key": host, "source_kind": "web",
           "prep_time": "5 mins", "prep_min_minutes": 5, "prep_max_minutes": 5, "prep_note": None,
           "cook_time": None, "cook_min_minutes": None, "cook_max_minutes": None, "cook_note": None}
    row.update(over)
    return row


def _fetcher(pages):
    def fetch(url):
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page
    return fetch


def test_a_stated_total_less_the_prep_fills_cook():
    row = _web()
    changes, report = mod.plan_fetch_times(
        [row], _fetcher({row["source_url"]: _page(prepTime="PT5M", totalTime="PT35M")}), sleep=lambda _s: None)
    [c] = changes
    assert c.new == {"cook_min_minutes": 30, "cook_max_minutes": 30, "cook_note": None}
    assert c.old == {"cook_min_minutes": None, "cook_max_minutes": None, "cook_note": None}
    assert report[("example.com", "filled")] == 1


def test_a_stated_cook_time_fills_cook_and_its_text():
    row = _web()
    [c], _ = mod.plan_fetch_times(
        [row], _fetcher({row["source_url"]: _page(cookTime="PT1H")}), sleep=lambda _s: None)
    assert c.new["cook_time"] == "1 hr"
    assert (c.new["cook_min_minutes"], c.new["cook_max_minutes"]) == (60, 60)
    assert c.old["cook_time"] is None
    assert not any(k.startswith("prep") for k in c.new)


def test_prep_is_filled_only_when_the_row_has_none():
    row = _web(prep_time=None, prep_min_minutes=None, prep_max_minutes=None)
    [c], _ = mod.plan_fetch_times(
        [row], _fetcher({row["source_url"]: _page(prepTime="PT10M", cookTime="PT20M")}), sleep=lambda _s: None)
    assert c.new["prep_time"] == "10 min"
    assert (c.new["prep_min_minutes"], c.new["prep_max_minutes"], c.new["prep_note"]) == (10, 10, None)
    assert (c.new["cook_min_minutes"], c.new["cook_time"]) == (20, "20 min")


def test_a_refused_or_empty_page_is_reported_not_written():
    rows = [_web("a"), _web("b"), _web("c")]
    pages = {rows[0]["source_url"]: SiteRefusedError("403"), rows[1]["source_url"]: UrlFetchError("timeout"),
             rows[2]["source_url"]: "<html><body>no recipe</body></html>"}
    changes, report = mod.plan_fetch_times(rows, _fetcher(pages), sleep=lambda _s: None)
    assert changes == []
    assert (report[("example.com", "refused")], report[("example.com", "fetch-failed")],
            report[("example.com", "no-times")]) == (1, 1, 1)


def test_a_row_that_already_has_a_cook_is_not_fetched():
    row = _web(cook_min_minutes=20, cook_max_minutes=20)
    changes, report = mod.plan_fetch_times([row], _fetcher({}), sleep=lambda _s: None)
    assert changes == [] and sum(report.values()) == 0


def test_fetches_are_paced():
    rows = [_web("a"), _web("b"), _web("c")]
    pauses = []
    mod.plan_fetch_times(rows, _fetcher({r["source_url"]: _page() for r in rows}), pause_s=1.0, sleep=pauses.append)
    assert pauses == [1.0, 1.0]


# ── final review fixes ─────────────────────────────────────────────────────────

def test_totals_leaves_rows_created_on_or_after_the_cutoff():
    # A fresh import since 9.48.1 already stores cook = total - prep with no cook text: the same shape.
    old = _times(id="old", created_at="2026-11-20T10:00:00+00:00")
    new = _times(id="new", created_at="2026-11-27T18:00:00+00:00")
    changes = mod.plan_totals([old, new], created_before="2026-11-27T17:00:00+00:00")
    assert [c.recipe_id for c in changes] == ["old"]


def test_totals_needs_a_created_before_cutoff_on_the_command_line(monkeypatch):
    monkeypatch.setattr(mod, "_client", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(SystemExit):
        mod.main(["totals"])


def test_a_web_row_with_no_source_kind_is_fetched():
    row = _web(source_kind=None)
    changes, report = mod.plan_fetch_times(
        [row], _fetcher({row["source_url"]: _page(cookTime="PT1H")}), sleep=lambda _s: None)
    assert len(changes) == 1


def test_a_book_row_is_not_fetched():
    row = _web(source_kind="book")
    changes, report = mod.plan_fetch_times([row], _fetcher({}), sleep=lambda _s: None)
    assert changes == [] and sum(report.values()) == 0


def test_an_unexpected_error_on_one_page_is_reported_and_the_run_goes_on():
    rows = [_web("a"), _web("b")]
    pages = {rows[0]["source_url"]: RuntimeError("odd page"), rows[1]["source_url"]: _page(cookTime="PT1H")}
    changes, report = mod.plan_fetch_times(rows, _fetcher(pages), sleep=lambda _s: None)
    assert [c.recipe_id for c in changes] == ["b"]
    assert report[("example.com", "error")] == 1


def test_the_sql_is_pure_ascii_so_any_shell_carries_it():
    c = RowChange("11111111-1111-1111-1111-111111111111",
                  {"source_title": "https://example.com/purée", "structured_ingredients": [{"n": "purée ½"}]},
                  {"source_title": "Délia's \\ 𝄞", "structured_ingredients": [{"n": "purée ½ 8"}]}, "n")
    sql = mod.render_sql([c], label="x", commit=False)
    assert sql.isascii()
    assert mod.sql_literal("Délia's \\ 𝄞", "text") == r"U&'D\00e9lia''s \\ \+01D11E'"
    assert mod.sql_literal("plain", "text") == "'plain'"
