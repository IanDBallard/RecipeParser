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


# ---------------------------------------------------------------------------
# The unparseable-text fallback
#
# parse_duration's success paths all run through _normalise, which collapses
# whitespace. Its failure path returned the raw input, so junk in a source
# recipe's prep_time/cook_time reached cook_note verbatim. Three rows in the
# live library carried 5.5k, 10k and 37k characters of newline padding that
# way -- and cook_note is a synced column, so a backfill would have shipped
# ~53KB of it to every device permanently.
# ---------------------------------------------------------------------------

def test_unparseable_note_collapses_runs_of_whitespace():
    """The real row: a number, thousands of newlines, then a stray token."""
    junk = "35" + "\n" * 6000 + "ext{"
    span = parse_duration(junk)
    assert span.min is None and span.max is None
    assert span.note == "35 ext{"


def test_unparseable_note_keeps_case_and_glyphs():
    """The note is shown to a cook, so it is tidied, not normalised: _normalise
    also lowercases and rewrites fractions, which would mangle the display."""
    assert parse_duration("About 1½ Hours, Roughly").note == "About 1½ Hours, Roughly"


def test_parseable_durations_are_unaffected_by_the_tidy():
    """The fallback is the only path that changed; a parse still yields no note."""
    assert parse_duration("1-2 hours") == Span(60, 120, None)


def test_unparseable_servings_note_collapses_runs_of_whitespace():
    """parse_servings carried the identical defect on its own failure path.
    The backfill cannot reach it -- it derives servings text from the numeric
    base_servings column -- but SupabaseWriter passes real recipe text through
    duration_columns on every ingest, so servings_note leaked the same way."""
    span = parse_servings("serves\n\n\n\n  a\t\tcrowd  ")
    assert span.min is None and span.max is None
    assert span.note == "serves a crowd"
