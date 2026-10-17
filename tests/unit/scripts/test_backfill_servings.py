"""The servings backfill's planning, with no database and no model."""
import csv
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("backfill_servings", ROOT / "scripts" / "backfill_servings.py")
bs = importlib.util.module_from_spec(spec)
sys.modules["backfill_servings"] = bs
spec.loader.exec_module(bs)

TIN = {"id": "pan_01", "raw": "two 20 cm tins", "count": 2, "family": "round-cake", "dims_mm": {"d": 200}}


def row(**over):
    base = {
        "id": "r1", "title": "Cake", "body_rev": 2, "derived_rev": 2,
        "ingredient_lines": ["200g flour"],
        "tokenized_directions": [{"step": 1, "text": "Mix."}, {"step": 2, "text": "Bake."}],
        "servings_min": 1, "servings_max": 1, "servings_note": "cake",
        "base_servings": 1, "servings_source": None, "pans": [], "bake_type": None, "cook_servings": None,
    }
    base.update(over)
    return base


def asker(n, calls=None):
    def ask(title, ingredients, directions):
        if calls is not None:
            calls.append(title)
        return n
    return ask


def no_ask(title, ingredients, directions):
    raise AssertionError("ask must not be called")


def test_stated_rows_are_never_touched():
    r = bs.plan_row(row(servings_source="stated", servings_min=4, servings_note="servings"), no_ask, force=True)
    assert r.status == "stated"


def test_stale_wins():
    assert bs.plan_row(row(derived_rev=1, servings_source="stated"), no_ask).status == "stale"


def test_counted_from_note_sets_without_the_model():
    r = bs.plan_row(row(servings_min=24, servings_max=24, servings_note="cookies", base_servings=1), no_ask)
    assert (r.status, r.source, r.new_base, r.old_base, r.used_model) == ("set", "counted", 24, 1, False)


def test_bake_with_tin_sets_tin():
    r = bs.plan_row(row(pans=[TIN], bake_type="butter-cake"), no_ask)
    assert (r.status, r.source, r.new_base) == ("set", "tin", 8)


def test_bake_without_tin_asks_the_model():
    calls = []
    r = bs.plan_row(row(), asker(12, calls))
    assert (r.status, r.source, r.new_base, r.used_model) == ("model", "estimated", 12, True)
    assert calls == ["Cake"]


def test_unplaced_when_the_model_has_nothing():
    r = bs.plan_row(row(), asker(None))
    assert r.status == "unplaced" and r.new_base is None


def test_error_when_the_model_raises():
    def boom(t, i, d):
        raise RuntimeError("quota")
    r = bs.plan_row(row(), boom)
    assert r.status == "error" and "quota" in r.notes[0]


def test_unchanged_when_nothing_moves():
    r = bs.plan_row(row(servings_min=24, servings_max=24, servings_note="cookies",
                        base_servings=24, servings_source="counted"), no_ask)
    assert r.status == "unchanged"


def test_cook_servings_rescales():
    r = bs.plan_row(row(servings_min=16, servings_max=16, servings_note="cookies",
                        base_servings=1, cook_servings=1.5), no_ask)
    assert r.new_base == 16 and r.new_cook == 24.0


def test_cook_servings_left_alone_without_an_old_base():
    r = bs.plan_row(row(servings_min=16, servings_max=16, servings_note="cookies",
                        base_servings=None, cook_servings=1.5), no_ask)
    assert r.new_cook == 1.5


class FakeTable:
    def __init__(self, data, log):
        self.data, self.log, self.filters = data, log, []

    def update(self, payload):
        self.log.append(("update", payload, self.filters))
        return self

    def select(self, cols):
        self.log.append(("select", cols, self.filters))
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def execute(self):
        return SimpleNamespace(data=self.data)


def test_write_row_is_conditional_and_omits_cook_when_unchanged():
    log = []
    sb = SimpleNamespace(table=lambda name: FakeTable([{"id": "r1"}], log))
    r = bs.plan_row(row(servings_min=24, servings_max=24, servings_note="cookies"), no_ask)
    assert bs.write_row(sb, row(), r) is True
    _, payload, filters = log[0]
    assert set(payload) == {"base_servings", "servings_source"}
    assert filters == [("id", "r1"), ("body_rev", 2), ("derived_rev", 2)]
    r = bs.plan_row(row(servings_min=24, servings_max=24, servings_note="cookies", cook_servings=1.5), no_ask)
    bs.write_row(sb, row(cook_servings=1.5), r)
    assert set(log[1][1]) == {"base_servings", "servings_source", "cook_servings"}
    assert bs.write_row(SimpleNamespace(table=lambda name: FakeTable([], [])), row(), r) is False


def test_review_flags_a_big_move(tmp_path):
    big = bs.plan_row(row(servings_min=16, servings_max=16, servings_note="cookies", base_servings=1), no_ask)
    small = bs.plan_row(
        row(id="r2", servings_min=10, servings_max=10, servings_note="cookies", base_servings=9), no_ask)
    path = tmp_path / "review.csv"
    bs.write_review(path, [big, small])
    with path.open(newline="", encoding="utf-8") as fh:
        lines = list(csv.DictReader(fh))
    assert list(lines[0]) == ["id", "title", "status", "old_base", "new_base", "source", "yield", "new_cook",
                              "used_model", "moved_pct", "flag"]
    assert lines[0]["flag"] == "check" and lines[0]["moved_pct"] == "1500"
    assert lines[1]["flag"] == ""


def _run_main(monkeypatch, argv, rows, log, estimate=12):
    import recipeparser
    import recipeparser.gemini as gem
    monkeypatch.setenv("SUPABASE_URL", "http://x")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(recipeparser, "client", object(), raising=False)
    monkeypatch.setattr(gem, "estimate_servings", lambda *a, **k: estimate)

    class Table(FakeTable):
        def select(self, cols):
            self.cols = cols.split(",")
            return self

        def order(self, col):
            return self

        def range(self, a, b):
            self.data = [{k: v for k, v in r.items() if k in self.cols} for r in rows] if a == 0 else []
            return self

    monkeypatch.setitem(sys.modules, "supabase", SimpleNamespace(
        create_client=lambda url, key: SimpleNamespace(table=lambda name: Table([{"id": "r1"}], log))))
    monkeypatch.setattr(sys, "argv", ["backfill_servings.py", *argv])
    return bs.main()


def test_live_requires_record(monkeypatch):
    log = []
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["--all-users", "--live"], [row()], log)
    assert "--record" in str(exc.value) and log == []


def test_dry_run_never_writes(monkeypatch):
    log = []
    assert _run_main(monkeypatch, ["--all-users"], [row()], log) == 0
    assert [e for e in log if e[0] == "update"] == []


def test_live_records_old_values_then_restore_writes_three_columns(monkeypatch, tmp_path):
    rec = tmp_path / "before.csv"
    old = row(cook_servings=1.5)
    log = []
    assert _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [old], log) == 0
    assert len([e for e in log if e[0] == "update"]) == 1
    with rec.open(newline="", encoding="utf-8") as fh:
        lines = list(csv.DictReader(fh))
    assert list(lines[0]) == ["id", "base_servings", "servings_source", "cook_servings"]
    assert lines[0]["base_servings"] == "1"
    log2 = []
    sb = SimpleNamespace(table=lambda name: FakeTable([], log2))
    assert bs._restore(sb, rec, live=False) == 0
    assert not log2
    assert bs._restore(sb, rec, live=True) == 0
    _, payload, filters = log2[0]
    assert payload == {"base_servings": 1, "servings_source": None, "cook_servings": 1.5}
    assert filters == [("id", "r1")]


def test_restore_writes_three_columns(tmp_path):
    rec = tmp_path / "r.csv"
    rec.write_text("id,base_servings,servings_source,cook_servings\nr9,,tin,\n", encoding="utf-8")
    log = []
    bs._restore(SimpleNamespace(table=lambda name: FakeTable([], log)), rec, live=True)
    assert log[0][1] == {"base_servings": None, "servings_source": "tin", "cook_servings": None}


def test_a_sourced_row_is_unchanged_without_force_and_asked_with_it():
    sourced = row(servings_source="estimated", base_servings=12)
    assert bs.plan_row(sourced, no_ask).status == "unchanged"
    calls = []
    r = bs.plan_row(sourced, asker(10, calls), force=True)
    assert calls == ["Cake"] and (r.status, r.new_base) == ("model", 10)
