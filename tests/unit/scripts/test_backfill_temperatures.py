"""The temperature backfill's planning, with no database and no model (Cayenne oven temperatures D16)."""
import importlib.util
import sys
from pathlib import Path

from recipeparser.models import TemperatureMention

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("backfill_temperatures", ROOT / "scripts" / "backfill_temperatures.py")
bt = importlib.util.module_from_spec(spec)
sys.modules["backfill_temperatures"] = bt
spec.loader.exec_module(bt)


def row(steps, **over):
    base = {
        "id": "r1", "title": "Roast", "body_rev": 2, "derived_rev": 2,
        "source_uom_system": None, "source_uom_system_detected": "UK",
        "tokenized_directions": [{"step": n, "text": t} for n, t in enumerate(steps, 1)],
    }
    base.update(over)
    return base


def test_tags_the_clear_forms_without_asking():
    asked = []
    result = bt.plan_row(row(["Mix the {{ing_01|flour|all}}.", "Bake at 180°C/160°C fan."]), lambda s, sys_: asked.append(s) or [])
    assert result.status == "tagged"
    assert result.directions[1].text == "Bake at {{temp:180C,oven,fan=160C|180°C/160°C fan}}."
    assert result.directions[0].text == "Mix the {{ing_01|flour|all}}."
    assert result.proposals == [(2, "180°C/160°C fan", "180C,oven,fan=160C", "rule")]
    assert asked == []


def test_asks_the_model_only_for_the_steps_it_cannot_settle_and_names_the_system():
    calls = []

    def ask(steps, system):
        calls.append((steps, system))
        return [TemperatureMention(step=1, quote="moderate oven", context="in a moderate oven", value="180C,oven")]

    result = bt.plan_row(row(["Bake at 200°C.", "Bake in a moderate oven."]), ask)
    assert calls == [(["Bake in a moderate oven."], "UK")]
    assert result.directions[1].text == "Bake in a {{temp:180C,oven|moderate oven}}."
    assert (2, "moderate oven", "180C,oven", "model") in result.proposals


def test_the_cook_s_system_wins_over_the_detected_one():
    calls = []
    bt.plan_row(row(["Bake at 350 for an hour."], source_uom_system="US"), lambda s, sys_: calls.append(sys_) or [])
    assert calls == ["US"]


def test_without_a_model_the_unsettled_steps_stay_as_they_are():
    result = bt.plan_row(row(["Bake in a moderate oven."]), None)
    assert result.status == "none-found"


def test_skips_stale_tagged_empty_and_malformed_rows():
    assert bt.plan_row(row(["Bake at 180°C."], derived_rev=1), None).status == "stale"
    assert bt.plan_row(row(["Bake at {{temp:180C,oven|180°C}}."]), None).status == "already-tagged"
    assert bt.plan_row(row(["Bake at {{temp:180C,oven|180°C}}."]), None, force=True).status == "none-found"
    assert bt.plan_row(row([]), None).status == "empty"
    assert bt.plan_row(row(["x"], tokenized_directions="[]"), None).status == "malformed"


def test_a_model_failure_fails_the_row_not_the_run():
    def ask(steps, system):
        raise RuntimeError("quota")
    result = bt.plan_row(row(["Bake in a moderate oven."]), ask)
    assert result.status == "failed" and "quota" in result.error


def test_no_word_changes():
    from recipeparser.core.fat_tokens import strip_fat_tokens
    steps = ["Preheat the oven to 425°F (220°C).", "Mix the {{ing_01|flour|all}} and bake at gas mark 4."]
    result = bt.plan_row(row(steps), None)
    assert [strip_fat_tokens(d.text) for d in result.directions] == [strip_fat_tokens(s) for s in steps]


def test_write_review_lists_every_proposal_with_who_made_it(tmp_path):
    import csv
    result = bt.plan_row(row(["Bake at 180°C/160°C fan."]), None)
    path = tmp_path / "review.csv"
    bt.write_review(path, [result])
    with path.open(newline="", encoding="utf-8") as fh:
        lines = list(csv.reader(fh))
    assert lines == [["id", "title", "step", "words", "value", "by"],
                     ["r1", "Roast", "1", "180°C/160°C fan", "180C,oven,fan=160C", "rule"]]


def test_repair_mends_existing_tokens_only():
    result = bt.plan_repair(row(["Heat oven to {{temp:350F,oven|350}} degrees.",
                                 "Heat to 500 to {{temp:550F,oven|550°}}F.",
                                 "Bake at {{temp:180C,oven|180°C}}."]))
    assert result.status == "tagged"
    assert [d.text for d in result.directions] == [
        "Heat oven to {{temp:350F,oven|350 degrees}}.", "Heat to 500 to 550°F.", "Bake at {{temp:180C,oven|180°C}}."]
    assert [p[3] for p in result.proposals] == ["repair", "repair"]
    assert bt.plan_repair(row(["Bake at {{temp:180C,oven|180°C}}."])).status == "none-found"
    assert bt.plan_repair(row(["Heat oven to {{temp:350F,oven|350}} degrees."], derived_rev=1)).status == "stale"
