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
