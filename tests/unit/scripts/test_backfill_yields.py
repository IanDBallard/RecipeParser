"""The yields backfill's planning, settlement, record and restore, with no database and no model
(Cayenne yield kinds D77, D83, 9, 11.4)."""
import csv
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from recipeparser.models import YieldAnswer, YieldContainer, YieldPieces

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("backfill_yields", ROOT / "scripts" / "backfill_yields.py")
by = importlib.util.module_from_spec(spec)
sys.modules["backfill_yields"] = by
spec.loader.exec_module(by)

INGREDIENTS = [
    {"id": "ing_01", "kind": "heading", "name": "For the pastry", "fallback_string": "For the pastry:"},
    {"id": "ing_02", "kind": "ingredient", "amount": 200, "unit": "g", "name": "flour",
     "fallback_string": "200 g flour"},
    {"id": "ing_03", "kind": "ingredient", "amount": 100, "unit": "g", "name": "butter",
     "fallback_string": "100 g butter"},
    {"id": "ing_04", "kind": "ingredient", "amount": 4, "unit": None, "name": "apples",
     "fallback_string": "4 apples"},
]


def row(**over):
    base = {
        "id": "r1", "title": "Pie", "body_rev": 2, "derived_rev": 2,
        "structured_ingredients": INGREDIENTS,
        "tokenized_directions": [
            {"step": 1, "text": "Line a {{pan_01|9-inch pie plate}} with the pastry."},
            {"step": 2, "text": "Bake at {{temp:200C|200°C}}."},
        ],
        "pans": [{"id": "pan_01", "raw": "9-inch pie plate", "shape": "round", "family": "pie", "dims_mm": {"d": 229}}],
        "bake_type": "pie-fruit",
        "yield_spec": None, "cook_servings": None, "cook_pans": None, "cook_scale_by": None, "cook_yield": None,
        "base_servings": 8, "source_uom_system": None, "source_uom_system_detected": "US",
    }
    base.update(over)
    return base


def servings_answer():
    return YieldAnswer(kind="servings", container=YieldContainer(
        pastry="shortcrust", base=True, sides="full", top="full", thickness="standard",
        component_lines=[2, 3], topping_lines=[]))


def component_answer():
    return YieldAnswer(kind="component", container=YieldContainer(
        pastry="shortcrust", base=True, sides="full", top="none", thickness="standard"))


def pieces_answer(count=1):
    return YieldAnswer(kind="pieces", pieces=YieldPieces(count=count, noun="loaf", weight_g=900))


def asker(ans, calls=None):
    def ask(steps, ingredients, pans, bake_type, words):
        if calls is not None:
            calls.append((steps, ingredients, pans, bake_type, words))
        return ans
    return ask


def no_ask(steps, ingredients, pans, bake_type, words):
    raise AssertionError("ask must not be called")


# --- settle_scale: section 9's table ---------------------------------------------------------------

COMPONENT = {"kind": "component", "container": {}, "area": None, "pieces": None, "edited": False}
BREAD = {"kind": "pieces", "container": None, "area": None,
         "pieces": {"count": 1, "noun": "loaf", "weight_g": 900, "diameter_mm": None}, "edited": False}


def test_servings_is_unchanged():
    s = {"kind": "servings", "container": None, "area": None, "pieces": None, "edited": False}
    assert by.settle_scale("servings", s, 12, [{"key": "x", "count": 1}], "pans", None, 8) == {}


def test_component_with_a_tin_keeps_the_tin():
    out = by.settle_scale("component", COMPONENT, 12, [{"key": "pie-9in", "count": 2}], "pans", None, 8)
    assert out == {"cook_servings": None, "cook_scale_by": None}


def test_the_trigger_crust_is_cleared():
    # cook_servings 47 against a base of 16, no tin: D83 clears it.
    out = by.settle_scale("component", COMPONENT, 47, None, "feed", None, 16)
    assert out == {"cook_servings": None, "cook_scale_by": None}  # cook_pans already null: not a change
    stray = by.settle_scale("component", COMPONENT, 47, [], "feed", None, 16)  # an empty tin list is no tin
    assert stray == {"cook_servings": None, "cook_pans": None, "cook_scale_by": None}


def test_component_with_nothing_saved_is_unchanged():
    assert by.settle_scale("component", COMPONENT, None, None, None, None, 8) == {}


def test_only_keys_that_change_are_returned():
    # a tin kept with no servings and no scale_by: nothing to write
    assert by.settle_scale("component", COMPONENT, None, [{"key": "pie-9in", "count": 1}], None, None, 8) == {}


def test_a_bread_at_two_becomes_two_loaves():
    out = by.settle_scale("pieces", BREAD, 2, None, "feed", None, 1)
    assert out == {"cook_yield": {"pieces": {"count": 2, "weight_g": 900, "diameter_mm": None}},
                   "cook_servings": None}


def test_pieces_count_rounds_half_up_and_never_below_one():
    rolls = {**BREAD, "pieces": {"count": 12, "noun": "roll", "weight_g": None, "diameter_mm": 80}}
    assert by.settle_scale("pieces", rolls, 5, None, None, None, 8)["cook_yield"]["pieces"]["count"] == 8  # 7.5
    assert by.settle_scale("pieces", rolls, 0.1, None, None, None, 8)["cook_yield"]["pieces"]["count"] == 1


def test_pieces_null_or_non_positive_base_reads_as_one():
    for base in (None, 0, -3):
        out = by.settle_scale("pieces", BREAD, 3, None, None, None, base)
        assert out["cook_yield"]["pieces"]["count"] == 3


def test_pieces_merges_into_an_existing_cook_yield_and_clears_the_tin():
    out = by.settle_scale("pieces", BREAD, 2, [{"key": "loaf-2lb", "count": 1}], None,
                          {"filling": "keep", "pieces": {"count": 9}}, 1)
    assert out["cook_yield"] == {"filling": "keep", "pieces": {"count": 2, "weight_g": 900, "diameter_mm": None}}
    assert out["cook_pans"] is None and out["cook_servings"] is None


def test_pieces_with_no_count_or_nothing_saved_is_unchanged():
    no_count = {**BREAD, "pieces": None}
    assert by.settle_scale("pieces", no_count, 2, None, None, None, 1) == {}
    assert by.settle_scale("pieces", BREAD, None, None, None, None, 1) == {}


# --- plan_row ----------------------------------------------------------------------------------------

def test_fresh_row_is_tagged_with_the_yields_only_inputs():
    calls = []
    r = by.plan_row(row(), asker(servings_answer(), calls))
    assert r.status == "tagged"
    assert r.spec["kind"] == "servings" and r.spec["container"]["component_lines"] == ["ing_02", "ing_03"]
    steps, ingredients, pans, bake_type, words = calls[0]
    assert steps == ["Line a 9-inch pie plate with the pastry.", "Bake at 200°C."]
    assert [i.id for i in ingredients] == ["ing_01", "ing_02", "ing_03", "ing_04"]  # headings included
    assert pans[0].id == "pan_01" and bake_type == "pie-fruit" and words is None
    assert r.settled == {}


def test_an_edited_spec_is_never_touched_even_forced():
    edited = row(yield_spec={"kind": "component", "edited": True})
    assert by.plan_row(edited, no_ask).status == "cook-edited"
    assert by.plan_row(edited, no_ask, force=True).status == "cook-edited"


def test_a_set_spec_is_already_done_unless_forced():
    done = row(yield_spec={"kind": "servings", "container": None, "area": None, "pieces": None, "edited": False})
    assert by.plan_row(done, no_ask).status == "already-done"
    assert by.plan_row(done, asker(component_answer()), force=True).status == "tagged"


def test_forced_with_the_same_answer_and_nothing_to_settle_is_none_found():
    first = by.plan_row(row(), asker(servings_answer()))
    again = by.plan_row(row(yield_spec=first.spec), asker(servings_answer()), force=True)
    assert again.status == "none-found"


def test_stale_malformed_failed_none_found():
    assert by.plan_row(row(derived_rev=1), no_ask).status == "stale"
    assert by.plan_row(row(tokenized_directions="x"), no_ask).status == "malformed"
    assert by.plan_row(row(structured_ingredients=[{"id": "x"}]), no_ask).status == "malformed"
    assert by.plan_row(row(pans=[{"raw": "no id"}]), no_ask).status == "malformed"
    assert by.plan_row(row(yield_spec="servings"), no_ask).status == "malformed"

    def boom(*a):
        raise RuntimeError("empty reply")
    r = by.plan_row(row(), boom)
    assert r.status == "failed" and "empty reply" in r.error
    assert by.plan_row(row(), asker(YieldAnswer(kind="cake"))).status == "none-found"


def test_a_component_row_carries_its_settlement_and_a_note():
    r = by.plan_row(row(cook_servings=47, base_servings=16, cook_scale_by="feed"), asker(component_answer()))
    assert r.settled == {"cook_servings": None, "cook_scale_by": None}  # cook_pans already null
    assert "cleared" in r.settled_text and "47" in r.settled_text and "no tin" in r.settled_text
    kept = by.plan_row(row(cook_servings=47, cook_pans=[{"key": "pie-9in", "count": 1}]), asker(component_answer()))
    assert kept.settled == {"cook_servings": None} and "tin kept" in kept.settled_text


def test_pieces_scale_is_left_for_stage_two_by_default():
    r = by.plan_row(row(cook_servings=2, base_servings=1), asker(pieces_answer()))
    assert r.status == "tagged" and r.settled == {}
    assert any("pieces scale left for stage 2" in n for n in r.notes)


def test_pieces_scale_converts_with_settle_pieces():
    r = by.plan_row(row(cook_servings=2, base_servings=1), asker(pieces_answer()), settle_pieces=True)
    assert r.settled["cook_yield"]["pieces"]["count"] == 2
    assert "converted" in r.settled_text


def test_pieces_with_no_count_are_demoted_to_servings_and_noted():
    ans = YieldAnswer(kind="pieces")
    r = by.plan_row(row(cook_servings=2, base_servings=1), asker(ans), settle_pieces=True)
    assert r.spec["kind"] == "servings"
    assert "yield pieces with no count stored as servings" in r.notes


def test_the_ask_is_given_the_yield_words():
    calls = []
    by.plan_row(row(servings_min=16, servings_max=16, servings_note="Enough for double crust"),
                asker(component_answer(), calls))
    assert calls[0][4] == "16 Enough for double crust"


def test_an_exception_after_the_answer_is_failed_not_fatal(monkeypatch):
    def broken(*a):
        raise KeyError("cook_yield")
    monkeypatch.setattr(by, "settle_scale", broken)
    r = by.plan_row(row(cook_servings=47), asker(component_answer()))
    assert r.status == "failed" and "cook_yield" in r.error and r.spec is None
    assert any("after the answer" in n for n in r.notes)


def test_a_pieces_conversion_already_in_cook_yield_does_not_raise():
    done = {"pieces": {"count": 2, "weight_g": 900, "diameter_mm": None}}
    r = by.plan_row(row(cook_servings=2, base_servings=1, cook_yield=done), asker(pieces_answer()),
                    settle_pieces=True)
    assert r.status == "tagged" and r.settled == {"cook_servings": None}
    assert "2 piece(s)" in r.settled_text


# --- write_row and the review ------------------------------------------------------------------------

IS_NULL = ("is", "null")


def _matches(stored, val):
    if val == IS_NULL:
        return stored is None
    if isinstance(stored, (dict, list)):
        return json.dumps(stored) == val
    return stored == val


class FakeTable:
    """A recipes table. With ``store`` (id -> the row as it is now), an update lands only when every filter
    matches the stored row, as PostgREST's would; without it, ``data`` is returned."""

    def __init__(self, data, log, store=None, raise_on_update=False):
        self.data, self.log, self.filters = data, log, []
        self.store, self.raise_on_update, self.updating = store, raise_on_update, False

    def update(self, payload):
        self.log.append(("update", payload, self.filters))
        self.updating = True
        return self

    def select(self, cols):
        self.log.append(("select", cols, self.filters))
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def is_(self, col, val):
        self.filters.append((col, ("is", val)))
        return self

    def execute(self):
        if self.updating and self.raise_on_update:
            raise RuntimeError("connection reset")
        if self.updating and self.store is not None:
            cur = self.store.get(dict(self.filters).get("id"))
            ok = cur is not None and all(_matches(cur.get(c), v) for c, v in self.filters)
            return SimpleNamespace(data=[{"id": cur["id"]}] if ok else [])
        return SimpleNamespace(data=self.data)


def test_write_row_is_conditional_on_the_revs_the_spec_and_the_saved_scale():
    log = []
    read = row(cook_servings=47, base_servings=16, cook_pans=[{"key": "pie-9in", "count": 2}])
    r = by.plan_row(read, asker(component_answer()))
    sb = SimpleNamespace(table=lambda name: FakeTable([{"id": "r1"}], log))
    assert by.write_row(sb, read, r) is True
    _, payload, filters = log[0]
    assert payload == {"yield_spec": r.spec, "cook_servings": None}
    assert filters == [("id", "r1"), ("body_rev", 2), ("derived_rev", 2), ("yield_spec", IS_NULL),
                       ("cook_servings", 47), ("cook_pans", json.dumps([{"key": "pie-9in", "count": 2}])),
                       ("cook_scale_by", IS_NULL), ("cook_yield", IS_NULL)]
    assert by.write_row(SimpleNamespace(table=lambda name: FakeTable([], [])), read, r) is False


def test_write_row_under_force_is_conditional_on_the_stored_spec():
    stored = {"kind": "servings", "container": None, "area": None, "pieces": None, "edited": False}
    read = row(yield_spec=stored)
    r = by.plan_row(read, asker(component_answer()), force=True)
    log = []
    by.write_row(SimpleNamespace(table=lambda name: FakeTable([{"id": "r1"}], log)), read, r)
    assert ("yield_spec", json.dumps(stored)) in log[0][2]


def test_review_columns_and_lines(tmp_path):
    tagged = by.plan_row(row(), asker(servings_answer()))
    skipped = by.plan_row(row(id="r0", derived_rev=1), no_ask)
    path = tmp_path / "review.csv"
    by.write_review(path, [tagged, skipped])
    with path.open(newline="", encoding="utf-8") as fh:
        lines = list(csv.reader(fh))
    assert lines[0] == ["id", "title", "status", "kind", "container", "area", "pieces", "component_lines",
                        "topping_lines", "settled", "notes"]
    assert lines[1][:3] == ["r0", "Pie", "stale"]
    assert lines[2][:4] == ["r1", "Pie", "tagged", "servings"]
    assert json.loads(lines[2][4])["top"] == "full"
    assert "ing_02: 200 g flour" in lines[2][7]


# --- main --------------------------------------------------------------------------------------------

MISSING = "column recipes.yield_spec does not exist"


def test_a_pieces_row_with_no_size_is_noted():
    sizeless = YieldAnswer(kind="pieces", pieces=YieldPieces(count=12, noun="roll"))
    r = by.plan_row(row(), asker(sizeless))
    assert r.status == "tagged" and "pieces: no size stated" in r.notes
    by_diameter = YieldAnswer(kind="pieces", pieces=YieldPieces(count=2, noun="pizza", diameter_mm=300))
    assert "pieces: no size stated" not in by.plan_row(row(), asker(by_diameter)).notes
    assert "pieces: no size stated" not in by.plan_row(row(), asker(pieces_answer())).notes
    assert "pieces: no size stated" not in by.plan_row(row(), asker(servings_answer())).notes


def test_a_pieces_answer_with_a_dropped_count_is_a_servings_row_with_no_size_note():
    answer = YieldAnswer(kind="pieces", pieces=YieldPieces(count=0, noun="pizza"))
    r = by.plan_row(row(), asker(answer))
    assert r.status == "tagged" and r.spec["kind"] == "servings"
    assert "pieces: no size stated" not in r.notes
    assert "yield pieces with no count stored as servings" in r.notes


def _run_main(monkeypatch, argv, rows, log, ans=None, missing_columns=False, writes_ok=True, moved=None,
              write_raises=False):
    """main() against a fake table. ``moved``: id -> columns changed after the read (a cook mid-run)."""
    import recipeparser
    import recipeparser.gemini as gem
    monkeypatch.setenv("SUPABASE_URL", "http://x")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(recipeparser, "client", object(), raising=False)
    asked = []

    def tag(*a, **k):
        asked.append((a, k))
        return ans or servings_answer()
    monkeypatch.setattr(gem, "tag_yield", tag)
    store = {r["id"]: {**r, **(moved or {}).get(r["id"], {})} for r in rows} if writes_ok else None

    class Table(FakeTable):
        def select(self, cols):
            self.cols = cols.split(",")
            self.log.append(("select", cols, self.filters))
            return self

        def order(self, col):
            return self

        def range(self, a, b):
            if missing_columns and ("yield_spec" in self.cols or "cook_yield" in self.cols):
                raise RuntimeError(f"{{'code': '42703', 'message': '{MISSING}'}}")
            self.data = [{k: v for k, v in r.items() if k in self.cols} for r in rows] if a == 0 else []
            return self

    monkeypatch.setitem(sys.modules, "supabase", SimpleNamespace(
        create_client=lambda url, key: SimpleNamespace(
            table=lambda name: Table([{"id": "r1"}] if writes_ok else [], log, store, write_raises))))
    monkeypatch.setattr(sys, "argv", ["backfill_yields.py", *argv])
    code = by.main()
    return code, asked


def _updates(log):
    return [e for e in log if e[0] == "update"]


def _record(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_live_without_record_refuses(monkeypatch):
    log = []
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["--all-users", "--live"], [row()], log)
    assert "--record" in str(exc.value) and log == []


def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    log = []
    review = tmp_path / "review.csv"
    code, asked = _run_main(monkeypatch, ["--all-users", "--review", str(review)],
                            [row(cook_servings=47, base_servings=16)], log, ans=component_answer())
    assert code == 0 and len(asked) == 1
    assert _updates(log) == []
    assert review.exists()


def test_main_passes_the_yield_words_to_tag_yield(monkeypatch):
    pie = row(servings_min=6, servings_max=8, servings_note="slices")
    _, asked = _run_main(monkeypatch, ["--all-users"], [pie], [])
    assert asked[0][1]["yield_text"] == "6 to 8 slices"


def test_dry_run_before_the_migration_reads_both_as_null(monkeypatch, capsys):
    log = []
    code, asked = _run_main(monkeypatch, ["--all-users"], [row()], log, missing_columns=True)
    out = capsys.readouterr().out
    assert code == 0 and len(asked) == 1
    assert "yield_spec/cook_yield are not on this database yet: dry run reads both as null" in out
    assert _updates(log) == []
    assert "yield_spec" not in log[-1][1] and "cook_yield" not in log[-1][1]
    assert "servings_note" in log[-1][1]


def test_live_before_the_migration_stops_before_asking_or_writing(monkeypatch, tmp_path):
    log = []
    rec = tmp_path / "before.csv"
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row()], log,
                  missing_columns=True)
    assert "yield_spec" in str(exc.value)
    assert _updates(log) == []
    assert not rec.exists()


def test_a_select_error_that_is_not_a_missing_column_is_raised(monkeypatch):
    import recipeparser
    monkeypatch.setenv("SUPABASE_URL", "http://x")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(recipeparser, "client", object(), raising=False)

    class Broken(FakeTable):
        def order(self, col):
            return self

        def range(self, a, b):
            raise RuntimeError("connection refused")

    monkeypatch.setitem(sys.modules, "supabase", SimpleNamespace(
        create_client=lambda url, key: SimpleNamespace(table=lambda name: Broken([], []))))
    monkeypatch.setattr(sys, "argv", ["backfill_yields.py", "--all-users"])
    with pytest.raises(RuntimeError, match="connection refused"):
        by.main()


def test_live_writes_the_spec_and_settlement_and_records_the_old_values(monkeypatch, tmp_path, capsys):
    log = []
    rec = tmp_path / "before.csv"
    crust = row(title="Crust", cook_servings=47, base_servings=16, cook_scale_by="feed",
                cook_yield={"filling": "keep"})
    code, _ = _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [crust], log,
                        ans=component_answer())
    assert code == 0
    (_, payload, filters), = _updates(log)
    assert payload["yield_spec"]["kind"] == "component"
    assert payload["cook_servings"] is None and payload["cook_scale_by"] is None
    assert "cook_pans" not in payload and "cook_yield" not in payload
    assert filters[:3] == [("id", "r1"), ("body_rev", 2), ("derived_rev", 2)]
    attempt, outcome = _record(rec)
    assert list(attempt) == ["id", "title", "yield_spec", "cook_servings", "cook_pans", "cook_scale_by",
                             "cook_yield", "written_yield_spec", "outcome"]
    assert attempt["yield_spec"] == "null" and attempt["cook_servings"] == "47"
    assert attempt["cook_scale_by"] == '"feed"' and attempt["cook_pans"] == "null"
    assert json.loads(attempt["cook_yield"]) == {"filling": "keep"}
    assert json.loads(attempt["written_yield_spec"]) == payload["yield_spec"]
    assert (attempt["outcome"], outcome["outcome"]) == ("attempted", "written")
    out = capsys.readouterr().out
    assert "cleared" in out and "Crust" in out


def test_record_is_on_disk_before_the_row_is_written(monkeypatch, tmp_path):
    rec = tmp_path / "before.csv"
    seen = []
    orig = FakeTable.update

    def update(self, payload):
        seen.append(rec.read_text(encoding="utf-8"))
        return orig(self, payload)

    monkeypatch.setattr(FakeTable, "update", update)
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row()], [])
    assert len(seen) == 1 and "r1" in seen[0] and "attempted" in seen[0]


def test_a_refused_write_is_raced_and_recorded(monkeypatch, tmp_path, capsys):
    rec = tmp_path / "before.csv"
    code, _ = _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row(cook_servings=47)], [],
                        ans=component_answer(), writes_ok=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "MOVED   r1" in out and "0 recipe(s), 1 moved during the run, 0 failed" in out
    assert [ln["outcome"] for ln in _record(rec)] == ["attempted", "raced"]


@pytest.mark.parametrize("moved", [
    {"cook_pans": [{"key": "pie-9in", "count": 1}]},  # a kitchen rescale to a tin mid-run
    {"cook_servings": 12},  # a kitchen rescale mid-run
    {"yield_spec": {"kind": "component", "container": None, "area": None, "pieces": None, "edited": True}},
])
def test_a_mid_run_change_is_refused_as_raced(monkeypatch, tmp_path, capsys, moved):
    rec = tmp_path / "before.csv"
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row(cook_servings=47)], [],
              ans=component_answer(), moved={"r1": moved})
    assert "1 moved during the run" in capsys.readouterr().out
    assert _record(rec)[-1]["outcome"] == "raced"


def test_an_unchanged_row_lands_through_the_guarded_fake(monkeypatch, tmp_path, capsys):
    rec = tmp_path / "before.csv"
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)],
              [row(cook_servings=2.94, cook_pans=[{"key": "pie-9in", "count": 2}], cook_scale_by="pans")], [],
              ans=component_answer())
    assert "wrote 1 recipe(s), 0 moved" in capsys.readouterr().out


def test_a_write_that_raises_is_failed_and_recorded(monkeypatch, tmp_path, capsys):
    rec = tmp_path / "before.csv"
    code, _ = _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row()], [],
                        write_raises=True)
    out = capsys.readouterr().out
    assert code == 1 and "FAILED to write r1" in out and "0 moved during the run, 1 failed" in out
    assert [ln["outcome"] for ln in _record(rec)] == ["attempted", "failed"]


def test_live_pieces_without_the_flag_changes_only_the_spec(monkeypatch, tmp_path):
    log = []
    bread = row(title="Bread", cook_servings=2, base_servings=1, cook_scale_by="feed")
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(tmp_path / "r.csv")], [bread], log,
              ans=pieces_answer())
    (_, payload, _), = _updates(log)
    assert set(payload) == {"yield_spec"}


def test_live_pieces_with_the_flag_becomes_two_loaves(monkeypatch, tmp_path):
    log = []
    bread = row(title="Bread", cook_servings=2, base_servings=1)
    _run_main(monkeypatch, ["--all-users", "--live", "--settle-pieces", "--record", str(tmp_path / "r.csv")],
              [bread], log, ans=pieces_answer())
    (_, payload, _), = _updates(log)
    assert payload["cook_yield"] == {"pieces": {"count": 2, "weight_g": 900, "diameter_mm": None}}
    assert payload["cook_servings"] is None


def test_ids_file_scopes(monkeypatch, tmp_path, capsys):
    ids = tmp_path / "scope.txt"
    ids.write_text("r2\n\n  r9 \n", encoding="utf-8")
    log = []
    code, asked = _run_main(monkeypatch, ["--all-users", "--ids-file", str(ids)],
                            [row(id="r1"), row(id="r2")], log)
    assert code == 0 and len(asked) == 1
    assert "2 id(s) listed, 1 read, 1 not found" in capsys.readouterr().out


def test_summary_counts_kinds_and_assemblies_without_lines(monkeypatch, capsys):
    ans = YieldAnswer(kind="servings", container=YieldContainer(pastry="puff", base=False, sides="none", top="full"))
    _run_main(monkeypatch, ["--all-users"], [row()], [], ans=ans)
    out = capsys.readouterr().out
    assert "servings" in out and "assemblies with no component lines: 1" in out


def test_summary_counts_pieces_with_no_size(monkeypatch, capsys):
    sizeless = YieldAnswer(kind="pieces", pieces=YieldPieces(count=12, noun="roll"))
    _run_main(monkeypatch, ["--all-users"], [row()], [], ans=sizeless)
    out = capsys.readouterr().out
    assert "pieces with no size: 1" in out
    _run_main(monkeypatch, ["--all-users"], [row()], [], ans=pieces_answer())
    assert "pieces with no size: 0" in capsys.readouterr().out


# --- restore -----------------------------------------------------------------------------------------

def _written(rec):
    return json.loads(_record(rec)[0]["written_yield_spec"])


def test_record_round_trips_through_restore_with_a_tin(monkeypatch, tmp_path):
    rec = tmp_path / "before.csv"
    tins = [{"key": "pie-9in", "count": 2, "label": "9 in pie plate"}]
    crust = row(cook_servings=47, base_servings=16, cook_scale_by="pans", cook_pans=tins,
                cook_yield={"filling": "keep"})
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [crust], [], ans=component_answer())
    written = _written(rec)
    log = []
    sb = SimpleNamespace(table=lambda name: FakeTable([{"yield_spec": written}], log))
    assert by._restore(sb, rec, live=False) == 0
    assert _updates(log) == []
    assert by._restore(sb, rec, live=True) == 0
    (_, payload, filters), = _updates(log)
    assert payload == {"yield_spec": None, "cook_servings": 47, "cook_pans": tins, "cook_scale_by": "pans",
                       "cook_yield": {"filling": "keep"}}
    assert filters == [("id", "r1"), ("yield_spec", json.dumps(written))]


def test_restore_skips_a_row_whose_current_spec_was_edited(monkeypatch, tmp_path, capsys):
    rec = tmp_path / "before.csv"
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row(cook_servings=47)], [],
              ans=component_answer())
    log = []
    sb = SimpleNamespace(table=lambda name: FakeTable([{"yield_spec": {"kind": "pieces", "edited": True}}], log))
    assert by._restore(sb, rec, live=True) == 0
    assert _updates(log) == []
    out = capsys.readouterr().out
    assert "SKIPPED r1" in out and "skipped 1 (1 edited)" in out


def test_restore_skips_a_row_changed_since(monkeypatch, tmp_path, capsys):
    rec = tmp_path / "before.csv"
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row(cook_servings=47)], [],
              ans=component_answer())
    regenerated = {**_written(rec), "kind": "servings"}
    log = []
    sb = SimpleNamespace(table=lambda name: FakeTable([{"yield_spec": regenerated}], log))
    assert by._restore(sb, rec, live=True) == 0
    assert _updates(log) == []
    assert "skipped 1 (1 changed since)" in capsys.readouterr().out


def test_restore_counts_a_conditional_update_that_finds_the_row_changed(monkeypatch, tmp_path, capsys):
    rec = tmp_path / "before.csv"
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row()], [])
    written = _written(rec)
    # the select sees the written spec; the guarded update finds it changed in between
    sb = SimpleNamespace(table=lambda name: FakeTable([{"yield_spec": written}], [], store={"r1": {"id": "r1"}}))
    assert by._restore(sb, rec, live=True) == 0
    assert "restored 0" in capsys.readouterr().out


@pytest.mark.parametrize("how", [{"writes_ok": False}, {"write_raises": True}])
def test_restore_skips_a_raced_or_failed_write(monkeypatch, tmp_path, capsys, how):
    rec = tmp_path / "before.csv"
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row(cook_servings=47)], [],
              ans=component_answer(), **how)
    capsys.readouterr()
    log = []
    sb = SimpleNamespace(table=lambda name: FakeTable([{"yield_spec": None}], log))
    assert by._restore(sb, rec, live=True) == 0
    assert _updates(log) == []
    out = capsys.readouterr().out
    assert "SKIPPED r1" in out and "skipped 1 (1 not written)" in out


def test_restore_skips_an_attempt_with_no_outcome(tmp_path, capsys):
    # a run killed between the attempt line and the write's outcome
    rec = tmp_path / "before.csv"
    with rec.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(by.RECORD_HEADER)
        w.writerow(["r1", "Pie", "null", "47", "null", "null", "null", json.dumps({"kind": "component"}),
                    "attempted"])
    log = []
    sb = SimpleNamespace(table=lambda name: FakeTable([{"yield_spec": {"kind": "component"}}], log))
    by._restore(sb, rec, live=True)
    out = capsys.readouterr().out
    assert _updates(log) == [] and "skipped 1 (1 not confirmed)" in out and "check it by hand" in out


def test_restore_through_main(monkeypatch, tmp_path):
    rec = tmp_path / "before.csv"
    _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row(cook_servings=47)], [],
              ans=component_answer())
    log = []
    _run_main(monkeypatch, ["--all-users", "--restore", str(rec)], [], log)
    assert _updates(log) == []


def test_an_existing_record_file_is_refused_before_the_model_or_the_database(monkeypatch, tmp_path):
    rec = tmp_path / "yields-before.csv"
    rec.write_text("the first run's undo\n", encoding="utf-8")
    log = []
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row()], log)
    message = str(exc.value)
    assert str(rec) in message and "new name" in message
    assert log == []
    assert rec.read_text(encoding="utf-8") == "the first run's undo\n"


def test_restore_help_says_a_later_kitchen_rescale_is_reverted(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["backfill_yields.py", "--help"])
    with pytest.raises(SystemExit):
        by.main()
    help_text = " ".join(capsys.readouterr().out.split())
    assert "rescale" in help_text and "reverted" in help_text
