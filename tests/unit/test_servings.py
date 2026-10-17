"""Yield classifier - every case in the shared fixture (D44, D47, D50)."""
import json
from pathlib import Path

import pytest

from recipeparser.core.servings import BAKE_NOUNS, ITEM_NOUNS, classify_servings

_CASES = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "servings_source_cases.json").read_text("utf-8")
)["cases"]


@pytest.mark.parametrize(
    "case", _CASES, ids=lambda c: f"{c['servings']!r}|{'/'.join(c['directions'])}"
)
def test_classify_fixture(case):
    got = classify_servings(case["servings"], case["directions"])
    assert (got.source, got.base) == (case["source"], case["base"])


def test_classify_keeps_span_note():
    assert classify_servings("Makes 24 cookies", []).span.note == "cookies"


def test_item_and_bake_nouns_are_disjoint():
    assert not set(ITEM_NOUNS) & set(BAKE_NOUNS)
