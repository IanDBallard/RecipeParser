"""Prompt and schema snapshots (spec §6.3).

The equivalence tests here are the refactor's safety net: each builder must
render exactly what the call site used to build inline.  The snapshots (added
in the next task) are what make prompt drift a reviewable diff.
"""
from __future__ import annotations

import json

import pytest
from syrupy.assertion import SnapshotAssertion

from recipeparser import gemini, shopping, toc
from recipeparser.models import (
    CayenneRefinement,
    DirectionMentions,
    RecipeExtraction,
    RecipeList,
    StructuredIngredient,
    TocList,
    TocRecipeClassification,
)
from recipeparser.shopping import ClassifyIngredient, ClassifyReply
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

PLACEHOLDER_STRUCTURED = [
    StructuredIngredient(id="ing_01", amount=2.0, unit="cups", name="plain flour",
                         fallback_string="2 cups/250g plain flour"),
    StructuredIngredient(id="ing_02", amount=1.0, unit="cup", name="sugar", fallback_string="1 cup sugar"),
]

PLACEHOLDER_INGREDIENTS = [
    ClassifyIngredient(key="r1:i1", text="2 cups plain flour", name="plain flour",
                       amount=2.0, unit="cup"),
    ClassifyIngredient(key="r1:i2", text="salt to taste", name="salt"),
]


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
    def test_extract(self, monkeypatch):
        sent = _sent_contents(
            monkeypatch, lambda c: gemini.extract_recipes(PLACEHOLDER_BODY, c)
        )
        assert sent == gemini.build_extract_prompt(PLACEHOLDER_BODY)

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

    def test_refine(self, monkeypatch):
        sent = _sent_contents(monkeypatch, lambda c: gemini.refine_recipe_for_cayenne(PLACEHOLDER_RECIPE, c))
        assert sent == gemini.build_refine_prompt(PLACEHOLDER_RECIPE, None)

    def test_categorize_batch(self, monkeypatch):
        rows = [{"id": "r1", "title": "Lasagne", "ingredient_lines": ["pasta"], "direction_steps": ["Bake."]}]
        sent = _sent_contents(monkeypatch, lambda c: gemini.categorize_batch(rows, {"Cuisine": ["Italian"]}, c))
        assert sent == gemini.build_categorize_batch_prompt(rows, {"Cuisine": ["Italian"]})

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

    def test_classify(self, monkeypatch):
        sent = _sent_contents(
            monkeypatch,
            lambda c: shopping.classify_ingredients(c, PLACEHOLDER_INGREDIENTS, ["flour"]),
        )
        assert sent == shopping.build_classify_prompt(PLACEHOLDER_INGREDIENTS, ["flour"])

    def test_direction_mentions(self, monkeypatch):
        steps = list(PLACEHOLDER_RECIPE.directions)
        sent = _sent_contents(
            monkeypatch, lambda c: gemini.tag_direction_mentions(PLACEHOLDER_STRUCTURED, steps, c)
        )
        assert sent == gemini.build_mentions_prompt(PLACEHOLDER_STRUCTURED, steps)

    def test_toc_parse_truncates_a_long_body(self):
        rendered = toc.build_toc_parse_prompt(["x" * 30_000])
        assert "[... truncated ...]" in rendered


class TestPromptSnapshots:
    def test_extract_prompt(self, snapshot: SnapshotAssertion):
        assert gemini.build_extract_prompt(PLACEHOLDER_BODY) == snapshot(name="extract")

    def test_plain_text_prompt(self, snapshot: SnapshotAssertion):
        assert gemini.build_plain_text_prompt(PLACEHOLDER_BODY) == snapshot

    def test_table_prompt(self, snapshot: SnapshotAssertion):
        assert gemini.build_table_prompt(PLACEHOLDER_BODY) == snapshot

    def test_refine_prompt(self, snapshot: SnapshotAssertion):
        # It asks for no tags since Cayenne Fix Roadmap F-246: the TAG stage does.
        assert gemini.build_refine_prompt(PLACEHOLDER_RECIPE, None) == snapshot

    def test_refine_prompt_with_a_host(self, snapshot: SnapshotAssertion):
        assert gemini.build_refine_prompt(PLACEHOLDER_RECIPE, "taste.com.au") == snapshot

    def test_categorize_batch_prompt_for_one_axis(self, snapshot: SnapshotAssertion):
        # The shape the import's TAG stage sends: one axis per call (F-246).
        assert gemini.build_categorize_batch_prompt(
            [{"id": "recipe-1", "title": "Lasagne", "ingredient_lines": ["pasta"], "direction_steps": ["Bake."]}],
            {"Cuisine": FIXED_AXES["Cuisine"]},
        ) == snapshot

    def test_direction_mentions_prompt(self, snapshot: SnapshotAssertion):
        assert gemini.build_mentions_prompt(PLACEHOLDER_STRUCTURED, list(PLACEHOLDER_RECIPE.directions)) == snapshot

    def test_toc_parse_prompt(self, snapshot: SnapshotAssertion):
        assert toc.build_toc_parse_prompt(["Contents", "Soups .... 3"]) == snapshot

    def test_toc_classify_prompt(self, snapshot: SnapshotAssertion):
        assert toc.build_toc_classify_prompt(["Soups", "Boiled Custard"]) == snapshot

    def test_classify_prompt(self, snapshot: SnapshotAssertion):
        assert shopping.build_classify_prompt(PLACEHOLDER_INGREDIENTS, ["flour"]) == snapshot

    def test_categorize_batch_prompt(self, snapshot: SnapshotAssertion):
        assert gemini.build_categorize_batch_prompt(
            [{"id": "r1", "title": "Lasagne", "ingredient_lines": ["pasta"], "direction_steps": ["Bake."]}],
            FIXED_AXES,
        ) == snapshot


class TestSchemaSnapshots:
    """The only guard that additionalProperties cannot creep back into a schema."""

    def test_recipe_list_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(RecipeList) == snapshot

    def test_cayenne_refinement_schema(self, snapshot: SnapshotAssertion):
        # What REFINE sends, which offers no field for tags (F-246).
        assert gemini.refine_json_schema() == snapshot

    def test_direction_mentions_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(DirectionMentions) == snapshot

    def test_toc_list_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(TocList) == snapshot

    def test_toc_classification_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(TocRecipeClassification) == snapshot

    def test_classify_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(ClassifyReply) == snapshot

    @pytest.mark.parametrize(
        "model", [RecipeList, CayenneRefinement, DirectionMentions, TocList, TocRecipeClassification, ClassifyReply]
    )
    def test_no_schema_carries_additional_properties(self, model):
        rendered = json.dumps(gemini._schema_for_gemini(model))
        assert "additionalProperties" not in rendered

