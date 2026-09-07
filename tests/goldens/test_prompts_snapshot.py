"""Prompt and schema snapshots (spec §6.3).

The equivalence tests here are the refactor's safety net: each builder must
render exactly what the call site used to build inline.  The snapshots (added
in the next task) are what make prompt drift a reviewable diff.
"""
from __future__ import annotations

import pytest

from recipeparser import gemini, toc
from recipeparser.models import RecipeExtraction
from tests.goldens.conftest import FIXED_AXES

PLACEHOLDER_BODY = "PLACEHOLDER CHUNK BODY — fixed text so the snapshot only moves when the template does."

PLACEHOLDER_RECIPE = RecipeExtraction(
    name="Placeholder Cake",
    photo_filename="cake.jpg",
    servings="8",
    prep_time="20 mins",
    cook_time="35 mins",
    ingredients=["2 cups/250g plain flour", "1 cup sugar"],
    directions=["Mix everything.", "Bake until done."],
)


def _sent_contents(monkeypatch, call) -> str:
    """The prompt a call site actually hands to generate_content."""
    seen = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen["contents"] = contents
            raise RuntimeError("captured")

    class _Client:
        models = _Models()

    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)
    try:
        call(_Client())
    except Exception:
        pass
    return seen["contents"]


class TestBuildersMatchTheCallSites:
    @pytest.mark.parametrize("units", ["book", "metric", "us", "imperial"])
    def test_extract(self, monkeypatch, units):
        sent = _sent_contents(
            monkeypatch, lambda c: gemini.extract_recipes(PLACEHOLDER_BODY, c, units=units)
        )
        assert sent == gemini.build_extract_prompt(PLACEHOLDER_BODY, units)

    def test_plain_text(self, monkeypatch):
        sent = _sent_contents(
            monkeypatch, lambda c: gemini.extract_recipe_from_text(PLACEHOLDER_BODY, c)
        )
        assert sent == gemini.build_plain_text_prompt(PLACEHOLDER_BODY)

    def test_table(self, monkeypatch):
        sent = _sent_contents(
            monkeypatch, lambda c: gemini.normalise_baker_table(PLACEHOLDER_BODY, c)
        )
        assert sent == gemini.build_table_prompt(PLACEHOLDER_BODY)

    @pytest.mark.parametrize(
        "axes,measure",
        [({}, "Volume"), (FIXED_AXES, "Volume"), (FIXED_AXES, "Weight")],
    )
    def test_refine(self, monkeypatch, axes, measure):
        sent = _sent_contents(
            monkeypatch,
            lambda c: gemini.refine_recipe_for_cayenne(
                PLACEHOLDER_RECIPE, c, measure_preference=measure, user_axes=axes
            ),
        )
        assert sent == gemini.build_refine_prompt(
            PLACEHOLDER_RECIPE, "US", measure, axes
        )

    def test_toc_parse(self, monkeypatch):
        chunks = ["Contents", "Soups .... 3", "Puddings .... 41"]
        sent = _sent_contents(
            monkeypatch, lambda c: toc._parse_toc_from_text_fallback(chunks, c)
        )
        assert sent == toc.build_toc_parse_prompt(chunks)

    def test_toc_classify(self, monkeypatch):
        entries = [("Soups", 3), ("Boiled Custard", 41)]
        sent = _sent_contents(
            monkeypatch, lambda c: toc._classify_toc_recipe_indices(entries, c)
        )
        assert sent == toc.build_toc_classify_prompt([e[0] for e in entries])

    def test_toc_parse_truncates_a_long_body(self):
        rendered = toc.build_toc_parse_prompt(["x" * 30_000])
        assert "[... truncated ...]" in rendered
