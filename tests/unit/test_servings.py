"""Yield classifier - every case in the shared fixture (D44, D47, D50)."""
import hashlib
import json
from pathlib import Path

import pytest

from recipeparser.core.servings import BAKE_NOUNS, CONTAINER_NOUNS, ITEM_NOUNS, classify_servings

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "servings_source_cases.json"
_CASES = json.loads(_FIXTURE.read_text("utf-8"))["cases"]
# The fixture is one file kept in two repositories: this constant also lives in Cayenne
# cayenne-web/tests/unit/domain/servingsSource.test.ts, and both change together with the fixture.
_FIXTURE_SHA256 = "68d24b54f4b4f3f989f95c572e6722b33a6f5cbfb4503e57fcef1a5b2cd2c34f"


def test_fixture_is_byte_identical_to_the_copy_cayenne_runs():
    assert hashlib.sha256(_FIXTURE.read_bytes()).hexdigest() == _FIXTURE_SHA256


@pytest.mark.parametrize(
    "case", _CASES, ids=lambda c: f"{c['servings']!r}|{'/'.join(c['directions'])}"
)
def test_classify_fixture(case):
    got = classify_servings(case["servings"], case["directions"])
    assert (got.source, got.base) == (case["source"], case["base"])


@pytest.mark.parametrize(
    "case", _CASES, ids=lambda c: f"{c['servings']!r}|{'/'.join(c['directions'])}"
)
def test_stored_form_never_reads_as_a_different_number(case):
    # The database keeps parse_servings' span, not the text: rebuilt, it must classify the same,
    # or fall to the tins and the model (bake/none) -- never to a different stated or counted number.
    from recipeparser.core.durations import parse_servings
    from recipeparser.core.servings import text_from_span

    raw = classify_servings(case["servings"], case["directions"])
    stored = classify_servings(text_from_span(parse_servings(case["servings"])), case["directions"])
    assert (stored.source, stored.base) == (raw.source, raw.base) or stored.source in ("bake", "none")


def test_classify_keeps_span_note():
    assert classify_servings("Makes 24 cookies", []).span.note == "cookies"


def test_item_and_bake_nouns_are_disjoint():
    assert not set(ITEM_NOUNS) & set(BAKE_NOUNS)


def test_container_nouns_are_neither_item_nor_bake_nouns():
    # D52: a container or volume keeps its own count; it is no longer a bake noun.
    assert not set(CONTAINER_NOUNS) & (set(BAKE_NOUNS) | set(ITEM_NOUNS))


@pytest.mark.parametrize(
    "text, noun",
    [("3-4 jars", "jars"), ("a jar", "jar"), ("3 × 1½ lb jars", "jars"), ("750 ml", "ml"),
     ("Makes about 1 litre", "litre"), ("1 bottle", "bottle")],
)
def test_container_yield_names_its_noun(text, noun):
    got = classify_servings(text, [])
    assert (got.source, got.noun) == ("counted", noun)


def test_stated_yield_has_no_noun():
    assert classify_servings("Serves 4 (makes 2 cups)", []).noun is None


@pytest.mark.parametrize(
    "text, noun",
    [("2 cups (about 24 truffles)", "truffles"), ("4 cups, makes 16 muffins", "muffins"),
     ("Makes 24 cookies", "cookies")],
)
def test_item_count_beats_container_and_names_its_noun(text, noun):
    # Coordinator ruling on D52: an item count beats a container count.
    got = classify_servings(text, [])
    assert (got.source, got.noun) == ("counted", noun)


def test_basins_and_moulds_are_bake_vessels():
    for noun in ("basin", "basins", "mould", "moulds", "mold", "molds", "pudding basin", "dish", "dishes"):
        assert noun in BAKE_NOUNS


# --- Composing the yield, the tins and the model (Task 3) ---------------------

from recipeparser.core.durations import Span  # noqa: E402
from recipeparser.core.servings import (  # noqa: E402
    Servings,
    servings_for_recipe,
    servings_for_restore,
    text_from_span,
)
from recipeparser.models import PanDims, RecipePan  # noqa: E402

_TIN = [RecipePan(id="pan_01", raw="two 20 cm tins", count=2, family="round-cake", dims_mm=PanDims(d=200))]


def test_compose_stated_ignores_tins_and_model():
    assert servings_for_recipe("Serves 4", [], _TIN, "butter-cake", 12) == Servings(4, "stated")


def test_compose_counted_from_the_note():
    assert servings_for_recipe("Makes 24 cookies", [], _TIN, None, 12) == Servings(24, "counted")


def test_compose_container_never_reaches_tins_or_model():
    # D52: "3-4 jars" keeps its own count; a 40-serving model estimate is never used.
    assert servings_for_recipe("3-4 jars", [], _TIN, "butter-cake", 40) == Servings(3, "counted")


def test_compose_bake_takes_tin():
    assert servings_for_recipe("Makes one cake", [], _TIN, "butter-cake", 12) == Servings(8, "tin")


def test_compose_bake_without_tin_takes_model():
    assert servings_for_recipe("Makes one cake", [], [], "butter-cake", 12) == Servings(12, "estimated")


def test_compose_none_without_anything_is_none():
    assert servings_for_recipe(None, [], [], None, None) == Servings(None, None)


def test_compose_none_with_only_the_model_is_estimated():
    assert servings_for_recipe(None, ["Mix."], [], None, 3) == Servings(3, "estimated")


def test_text_from_span():
    assert text_from_span(Span(10, 12, "as a starter")) == "10-12 as a starter"
    assert text_from_span(Span(4, 4, "cookies")) == "4 cookies"
    assert text_from_span(Span(4, None, None)) == "4"
    assert text_from_span(Span(None, None, "one 9-inch pan")) == "one 9-inch pan"
    assert text_from_span(Span(None, None, None)) == ""


def test_restore_never_estimates_with_the_model():
    assert servings_for_restore(Span(None, None, "one cake"), [], [], "butter-cake", 5) == Servings(5, None)


def test_restore_takes_tin():
    assert servings_for_restore(Span(1, 1, "9-inch pan"), [], _TIN, "butter-cake", 1) == Servings(8, "tin")


def test_restore_stated_keeps_the_exported_base_with_no_source():
    # A pre-9.17 archive's number reads as the recipe's own (item 11): base stays, source None.
    assert servings_for_restore(Span(10, 12, "as a starter"), [], _TIN, None, 8) == Servings(8, None)


def test_restore_counted_keeps_the_exported_base_with_no_source():
    assert servings_for_restore(Span(24, 24, "cookies"), [], _TIN, None, 20) == Servings(20, None)
