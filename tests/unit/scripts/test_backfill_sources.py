"""The backfill's planning function, against the literal strings measured on 2026-09-11."""
import importlib.util
from collections import Counter
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "backfill_sources", Path(__file__).parents[3] / "scripts" / "backfill_sources.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _row(i, source, kind=None):
    return {"id": f"r{i}", "title": f"Recipe {i}", "source": source, "source_kind": kind}


MEASURED = [
    None,                                        # 1
    "The Food of Sichuan — Fuchsia Dunlop",      # 2
    "EPUB Auto-Import",                          # 3
    "cooking.nytimes.com",                       # 4
    "cooking.nytimes.com",                       # 4
    "Cooking.nytimes.com",                       # 4  (the minority spelling)
    "www.thewoksoflife.com",                     # 4
    "thewoksoflife.com",                         # 4
    "Womanandhome.com Justin Gellatly",          # 5
    "Gemini",                                    # 6
    "Nik Sharma",                                # 7
    "Perfect",                                   # 7
]


def test_every_rule_fires_once_at_least_and_counts_are_reported():
    plan = mod.plan_backfill([_row(i, s) for i, s in enumerate(MEASURED)])
    assert len(plan) == len(MEASURED)
    assert Counter(rule for _, _, rule in plan) == {1: 1, 2: 1, 3: 1, 4: 5, 5: 1, 6: 1, 7: 2}


def test_book_row_columns():
    [(rid, cols, rule)] = mod.plan_backfill([_row(0, "The Food of Sichuan — Fuchsia Dunlop")])
    assert cols == {"source_kind": "book", "source_key": "the food of sichuan",
                    "source_title": "The Food of Sichuan", "source_author": "Fuchsia Dunlop"}
    assert "source" not in cols  # the display string is untouched


def test_site_title_is_the_majority_spelling_and_one_key():
    rows = [_row(0, "cooking.nytimes.com"), _row(1, "cooking.nytimes.com"), _row(2, "Cooking.nytimes.com")]
    plan = mod.plan_backfill(rows)
    assert {cols["source_key"] for _, cols, _ in plan} == {"cooking.nytimes.com"}
    assert {cols["source_title"] for _, cols, _ in plan} == {"cooking.nytimes.com"}
    rows = [_row(0, "www.thewoksoflife.com"), _row(1, "www.thewoksoflife.com"), _row(2, "thewoksoflife.com")]
    plan = mod.plan_backfill(rows)
    assert {cols["source_key"] for _, cols, _ in plan} == {"thewoksoflife.com"}
    assert {cols["source_title"] for _, cols, _ in plan} == {"www.thewoksoflife.com"}


def test_gemini_is_cleared():
    [(_, cols, rule)] = mod.plan_backfill([_row(0, "Gemini")])
    assert rule == 6
    assert cols == {"source_kind": "unknown", "source_key": None, "source_title": None, "source_author": None,
                    "source": None}


def test_unknown_book_and_no_source_are_distinct_pills():
    plan = mod.plan_backfill([_row(0, "EPUB Auto-Import"), _row(1, None)])
    assert plan[0][1]["source_key"] == "unknown-book"
    assert plan[1][1]["source_key"] is None
    assert plan[0][1]["source_kind"] == plan[1][1]["source_kind"] == "unknown"


def test_a_row_already_classified_is_skipped_unless_forced():
    rows = [_row(0, "Nik Sharma", kind="person"), _row(1, "Nik Sharma")]
    assert [rid for rid, _, _ in mod.plan_backfill(rows)] == ["r1"]
    assert [rid for rid, _, _ in mod.plan_backfill(rows, force=True)] == ["r0", "r1"]
