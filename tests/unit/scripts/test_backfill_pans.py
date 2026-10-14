"""The pan backfill's planning, with no database and no model (Cayenne pan-aware scaling D22)."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from recipeparser.models import CoreMention, PanAnswer, PanDims, PanMention, RecipePan

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("backfill_pans", ROOT / "scripts" / "backfill_pans.py")
bp = importlib.util.module_from_spec(spec)
sys.modules["backfill_pans"] = bp
spec.loader.exec_module(bp)

STEPS = [
    "Grease a 20cm round tin.",
    "Bake until the middle reaches {{temp:93C|93°C}}.",
]


def row(steps=STEPS, **over):
    base = {
        "id": "r1", "title": "Cake", "body_rev": 2, "derived_rev": 2,
        "ingredient_lines": ["200g flour"], "structured_ingredients": [],
        "source_uom_system": None, "source_uom_system_detected": "UK",
        "pans": [], "bake_type": None, "bake_step": None,
        "tokenized_directions": [{"step": n, "text": t} for n, t in enumerate(steps, 1)],
    }
    base.update(over)
    return base


def tin(**over):
    base = dict(id="pan_01", raw="20cm round tin", shape="round", family="round-cake", dims_mm=PanDims(d=200))
    base.update(over)
    return RecipePan(**base)


def answer(**over):
    base = dict(
        pans=[tin()], bake_type="sponge", bake_step=1,
        mentions=[PanMention(step=1, quote="20cm round tin", context="Grease a 20cm round tin", pan_id="pan_01")],
        core=[CoreMention(step=2, quote="93°C")],
    )
    base.update(over)
    return PanAnswer(**base)


def asker(ans, calls=None):
    def ask(steps, ingredients, system):
        if calls is not None:
            calls.append((steps, ingredients, system))
        return ans
    return ask


def no_ask(steps, ingredients, system):
    raise AssertionError("ask must not be called")


def test_fresh_row_is_tagged():
    calls = []
    r = bp.plan_row(row(), asker(answer(), calls))
    assert r.status == "tagged"
    assert r.directions[0].text == "Grease a {{pan_01|20cm round tin}}."
    assert r.directions[1].text == "Bake until the middle reaches {{temp:93C,core|93°C}}."
    assert r.pans[0]["id"] == "pan_01" and r.pans[0]["edited"] is False
    assert (r.bake_type, r.bake_step) == ("sponge", 1)
    assert calls == [(["Grease a 20cm round tin.", "Bake until the middle reaches 93°C."], ["200g flour"], "UK")]


def test_ingredients_fall_back_to_structured_fallback_strings():
    calls = []
    bp.plan_row(row(ingredient_lines=None, structured_ingredients=[{"fallback_string": "1 cup sugar"}]),
                asker(answer(), calls))
    assert calls[0][1] == ["1 cup sugar"]


@pytest.mark.parametrize("over", [
    {"bake_type": "sponge"},
    {"pans": [{"id": "pan_01", "raw": "x"}]},
    {"tokenized_directions": [{"step": 1, "text": "Grease a {{pan_01|20cm round tin}}."}]},
])
def test_already_done_by_each_trigger_and_force_asks(over):
    assert bp.plan_row(row(**over), no_ask).status == "already-done"
    assert bp.plan_row(row(**over), asker(PanAnswer()), force=True).status == "tagged"


def test_stale_malformed_empty_failed():
    assert bp.plan_row(row(derived_rev=1), no_ask).status == "stale"
    assert bp.plan_row(row(tokenized_directions="[]"), no_ask).status == "malformed"
    assert bp.plan_row(row([]), no_ask).status == "empty"

    def boom(steps, ingredients, system):
        raise RuntimeError("quota")
    r = bp.plan_row(row(), boom)
    assert r.status == "failed" and "quota" in r.error


def test_no_tins_but_a_bake_type_is_still_tagged():
    r = bp.plan_row(row(), asker(PanAnswer(bake_type="pie-fruit", bake_step=1)))
    assert r.status == "tagged" and r.pans == [] and r.bake_type == "pie-fruit"


def test_cook_edited_tin_is_never_touched_even_forced():
    edited = row(pans=[{"id": "pan_01", "raw": "x", "edited": True}])
    assert bp.plan_row(edited, no_ask).status == "cook-edited"
    assert bp.plan_row(edited, no_ask, force=True).status == "cook-edited"


def test_force_unwraps_old_tokens_so_one_token_remains():
    steps = ["Grease a {{pan_01|20cm tin}}.", "Bake."]
    ans = answer(
        pans=[tin(raw="20cm tin")], core=[],
        mentions=[PanMention(step=1, quote="20cm tin", context="Grease a 20cm tin", pan_id="pan_01")])
    r = bp.plan_row(row(steps), asker(ans), force=True)
    assert r.directions[0].text == "Grease a {{pan_01|20cm tin}}."


def test_steps_out_of_range_and_unknown_tins_are_ignored():
    ans = answer(mentions=[
        PanMention(step=9, quote="x", context="", pan_id="pan_01"),
        PanMention(step=0, quote="x", context="", pan_id="pan_01"),
        PanMention(step=1, quote="20cm round tin", context="", pan_id="pan_07"),
    ], core=[CoreMention(step=-1, quote="93°C")])
    r = bp.plan_row(row(), asker(ans))
    assert r.status == "tagged"
    assert "pan_" not in r.directions[0].text
    assert sum("ignored" in n for n in r.notes) == 3
    assert r.directions[1].text.endswith("{{temp:93C|93°C}}.")


def test_drops_and_core_notes_are_collected():
    ans = answer(
        mentions=[PanMention(step=1, quote="nonesuch", context="", pan_id="pan_01")],
        core=[CoreMention(step=2, quote="nope")])
    r = bp.plan_row(row(), asker(ans))
    assert any("nonesuch" in n for n in r.notes) and any("nope" in n for n in r.notes)


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


def test_write_row_is_conditional_and_reports_a_race():
    log = []
    r = bp.plan_row(row(), asker(answer()))
    sb = SimpleNamespace(table=lambda name: FakeTable([{"id": "r1"}], log))
    assert bp.write_row(sb, row(), r) is True
    _, payload, filters = log[0]
    assert set(payload) == {"pans", "bake_type", "bake_step", "tokenized_directions"}
    assert filters == [("id", "r1"), ("body_rev", 2), ("derived_rev", 2)]
    assert bp.write_row(SimpleNamespace(table=lambda name: FakeTable([], [])), row(), r) is False


def test_review_lists_tokens_notes_and_skips(tmp_path):
    import csv
    r = bp.plan_row(row(), asker(answer()))
    skipped = bp.plan_row(row(derived_rev=1, id="r0"), no_ask)
    failed = bp.RowResult(id="r2", title="F", status="failed", error="quota")
    path = tmp_path / "review.csv"
    bp.write_review(path, [r, skipped, failed])
    with path.open(newline="", encoding="utf-8") as fh:
        lines = list(csv.reader(fh))
    assert lines[0] == ["id", "title", "status", "pans", "bake_type", "bake_step_1based", "step", "step_text",
                        "pan_tokens", "core_tokens", "notes"]
    assert lines[1][:3] == ["r0", "Cake", "stale"]
    assert [ln[5:10] for ln in lines[2:4]] == [
        ["2", "1", "Grease a {{pan_01|20cm round tin}}.", "pan_01: 20cm round tin", ""],
        ["2", "2", "Bake until the middle reaches {{temp:93C,core|93°C}}.", "", "93°C"]]
    assert lines[4][2] == "failed" and lines[4][-1] == "quota"


def test_stale_wins_over_malformed():
    assert bp.plan_row(row(derived_rev=1, tokenized_directions="x"), no_ask).status == "stale"


def test_guard_ignores_step_zero_and_negative_even_when_the_quote_is_in_the_last_step():
    ans = answer(
        mentions=[PanMention(step=0, quote="the middle", context="", pan_id="pan_01")],
        core=[CoreMention(step=0, quote="93°C")], bake_step=None)
    r = bp.plan_row(row(), asker(ans))
    assert r.directions[1].text == "Bake until the middle reaches {{temp:93C|93°C}}."
    assert sum("ignored: step 0" in n for n in r.notes) == 2


def test_force_unwrap_drops_stale_ids_and_differing_words():
    steps = ["Grease a {{pan_02|20cm tin}} and a {{pan_01|small tin}}.", "Bake."]
    ans = answer(
        pans=[tin(raw="20cm round tin")], core=[],
        mentions=[PanMention(step=1, quote="20cm tin", context="Grease a 20cm tin", pan_id="pan_01")])
    r = bp.plan_row(row(steps), asker(ans), force=True)
    assert r.directions[0].text == "Grease a {{pan_01|20cm tin}} and a small tin."
    assert "pan_02" not in r.directions[0].text


def _run_main(monkeypatch, argv, rows, log, ans=None):
    import recipeparser
    import recipeparser.gemini as gem
    monkeypatch.setenv("SUPABASE_URL", "http://x")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(recipeparser, "client", object(), raising=False)
    monkeypatch.setattr(gem, "tag_pans", lambda *a, **k: ans or answer())

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
    monkeypatch.setattr(sys, "argv", ["backfill_pans.py", *argv])
    return bp.main()


def test_dry_run_never_writes(monkeypatch):
    log = []
    assert _run_main(monkeypatch, ["--all-users"], [row()], log) == 0
    assert log == []


def test_live_without_record_refuses(monkeypatch):
    log = []
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["--all-users", "--live"], [row()], log)
    assert "--record" in str(exc.value) and log == []


def test_live_with_record_writes_and_records_the_old_values(monkeypatch, tmp_path):
    import csv
    log = []
    rec = tmp_path / "before.csv"
    old = row(bake_step=3, pans=[{"id": "pan_09", "raw": "old"}])
    assert _run_main(monkeypatch, ["--all-users", "--live", "--force", "--record", str(rec)], [old], log) == 0
    assert len(log) == 1
    with rec.open(newline="", encoding="utf-8") as fh:
        lines = list(csv.DictReader(fh))
    assert lines[0]["bake_step"] == "3" and lines[0]["bake_type"] == "null"
    assert lines[0]["pans"] == '[{"id": "pan_09", "raw": "old"}]'


def test_record_is_on_disk_before_the_row_is_written(monkeypatch, tmp_path):
    rec = tmp_path / "before.csv"
    seen = []
    orig = FakeTable.update

    def update(self, payload):
        seen.append(rec.read_text(encoding="utf-8"))
        return orig(self, payload)

    monkeypatch.setattr(FakeTable, "update", update)
    assert _run_main(monkeypatch, ["--all-users", "--live", "--record", str(rec)], [row()], []) == 0
    assert len(seen) == 1 and "r1" in seen[0]


def test_record_round_trips_through_restore(monkeypatch, tmp_path):
    rec = tmp_path / "before.csv"
    old = row(bake_step=3, pans=[{"id": "pan_09", "raw": "old"}])
    _run_main(monkeypatch, ["--all-users", "--live", "--force", "--record", str(rec)], [old], [])
    log = []
    sb = SimpleNamespace(table=lambda name: FakeTable([], log))
    assert bp._restore(sb, rec, live=False) == 0
    assert not [e for e in log if e[0] == "update"]
    assert bp._restore(sb, rec, live=True) == 0
    _, payload, filters = [e for e in log if e[0] == "update"][0]
    assert set(payload) == {"pans", "bake_type", "bake_step", "tokenized_directions"}
    assert filters == [("id", "r1")]
    assert payload["pans"] == [{"id": "pan_09", "raw": "old"}]
    assert payload["bake_type"] is None and payload["bake_step"] == 3
    assert payload["tokenized_directions"] == old["tokenized_directions"]


def test_restore_skips_a_row_whose_current_tin_was_edited(monkeypatch, tmp_path, capsys):
    rec = tmp_path / "before.csv"
    old = row(bake_step=3, pans=[{"id": "pan_09", "raw": "old"}])
    _run_main(monkeypatch, ["--all-users", "--live", "--force", "--record", str(rec)], [old], [])
    log = []
    now = [{"pans": [{"id": "pan_01", "raw": "20cm tin", "edited": True}]}]
    sb = SimpleNamespace(table=lambda name: FakeTable(now, log))
    assert bp._restore(sb, rec, live=True) == 0
    assert not [e for e in log if e[0] == "update"]
    out = capsys.readouterr().out
    assert "SKIPPED r1" in out and "skipped 1" in out


def test_a_recipe_with_nothing_found_is_none_found():
    ans = PanAnswer(pans=[], bake_type=None, bake_step=None)
    r = bp.plan_row(row(), asker(ans))
    assert r.status == "none-found"


def test_none_found_is_never_written_or_recorded(monkeypatch, tmp_path):
    import csv
    log = []
    rec, rev = tmp_path / "before.csv", tmp_path / "review.csv"
    monkeypatch.setattr(bp, "write_row", lambda *a, **k: (_ for _ in ()).throw(AssertionError("written")))
    argv = ["--all-users", "--live", "--record", str(rec), "--review", str(rev)]
    assert _run_main(monkeypatch, argv, [row(steps=["Stir.", "Serve."])], log, ans=PanAnswer()) == 0
    assert not [e for e in log if e[0] == "update"]
    with rec.open(newline="", encoding="utf-8") as fh:
        assert len(list(csv.reader(fh))) == 1  # header only
    with rev.open(newline="", encoding="utf-8") as fh:
        lines = list(csv.DictReader(fh))
    assert [(ln["id"], ln["status"]) for ln in lines] == [("r1", "none-found")]
