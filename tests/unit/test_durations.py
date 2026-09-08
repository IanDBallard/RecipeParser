"""Duration/servings parser — every case in the shared fixture (spec 3.6)."""
import json
from pathlib import Path

import pytest

from recipeparser.core.durations import Span, duration_columns, parse_duration, parse_servings

_CASES = json.loads((Path(__file__).parent.parent / "fixtures" / "duration_cases.json").read_text("utf-8"))


@pytest.mark.parametrize("case", _CASES["duration"], ids=lambda c: repr(c["input"]))
def test_parse_duration(case):
    assert parse_duration(case["input"]) == Span(case["min"], case["max"], case["note"])


@pytest.mark.parametrize("case", _CASES["servings"], ids=lambda c: repr(c["input"]))
def test_parse_servings(case):
    assert parse_servings(case["input"]) == Span(case["min"], case["max"], case["note"])


def test_duration_columns_shape():
    cols = duration_columns("15 mins", "1-2 hours", "2-4", fallback_base_servings=None)
    assert cols == {
        "prep_min_minutes": 15, "prep_max_minutes": 15, "prep_note": None,
        "cook_min_minutes": 60, "cook_max_minutes": 120, "cook_note": None,
        "servings_min": 2, "servings_max": 4, "servings_note": None,
        "base_servings": 2,
    }


def test_duration_columns_base_servings_fallback():
    cols = duration_columns(None, None, "a crowd", fallback_base_servings=6)
    assert cols["servings_min"] is None
    assert cols["servings_note"] == "a crowd"
    assert cols["base_servings"] == 6
