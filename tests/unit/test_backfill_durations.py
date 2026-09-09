import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "backfill_durations", Path(__file__).parents[2] / "scripts" / "backfill_durations.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_plan_backfill_parses_and_flags_note_only():
    rows = [
        {"id": "a", "prep_time": "15 mins", "cook_time": "overnight", "base_servings": 4},
        {"id": "b", "prep_time": None, "cook_time": None, "base_servings": None},
    ]
    plan = mod.plan_backfill(rows)
    assert plan[0][0] == "a"
    assert plan[0][1]["prep_min_minutes"] == 15
    assert plan[0][1]["cook_note"] == "overnight"
    assert plan[0][1]["base_servings"] == 4            # existing value kept
    assert plan[0][2] is True                          # cook text became note-only
    assert plan[1][2] is False
    assert "servings_min" in plan[1][1]


def test_fractional_base_servings_survives_the_backfill():
    # base_servings is a numeric column. int(base) turned 4.5 into 4 and the
    # UPDATE wrote that back, so a --live run silently rewrote every fractional
    # base_servings in the library. The original value must be passed through
    # untouched, and the structured servings columns still seeded from it.
    rows = [{"id": "a", "prep_time": "10 mins", "cook_time": None, "base_servings": 4.5}]
    cols = mod.plan_backfill(rows)[0][1]
    assert cols["base_servings"] == 4.5                # exactly what was read
    assert isinstance(cols["base_servings"], float)    # not coerced to int
    assert cols["servings_min"] is not None            # still seeded


def test_null_base_servings_stays_null():
    rows = [{"id": "a", "prep_time": None, "cook_time": None, "base_servings": None}]
    cols = mod.plan_backfill(rows)[0][1]
    assert cols["base_servings"] is None
    assert cols["servings_min"] is None and cols["servings_max"] is None


def test_servings_text_does_not_truncate():
    assert mod._servings_text(4.5) == "4.5"
    assert mod._servings_text(4.0) == "4"
    assert mod._servings_text(4) == "4"
    assert mod._servings_text(None) is None
