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
