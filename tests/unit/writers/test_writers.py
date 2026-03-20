"""
tests/unit/writers/test_writers.py — Phase 5 gate tests for RecipeWriter implementations.

Gate command: pytest tests/unit/writers/ -v

Five tests:
  1. test_supabase_writer_inserts_all_recipes
  2. test_supabase_writer_inserts_recipe_categories
  3. test_paprika_writer_produces_valid_zip
  4. test_cayenne_zip_writer_embeds_cayenne_meta
  5. test_round_trip_cayenne_zip_to_paprika_reader_is_zero_cost
"""
from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, call, patch

import pytest

from recipeparser.core.models import InputType
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.writers.cayenne_zip import CayenneZipWriter
from recipeparser.io.writers.paprika_zip import PaprikaWriter
from recipeparser.io.writers.supabase import SupabaseWriter
from recipeparser.models import IngestResponse, StructuredIngredient, TokenizedDirection

# ---------------------------------------------------------------------------
# Shared fixture factory
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 1536


def _make_embedding() -> List[float]:
    """Return a deterministic 1536-dim unit vector."""
    return [0.001 * (i % 100) for i in range(_EMBEDDING_DIM)]


def _make_recipe(
    title: str = "Test Pasta",
    fat_tokens: bool = False,
) -> IngestResponse:
    """
    Build a minimal but complete IngestResponse fixture.

    When ``fat_tokens=True`` the direction text contains a Fat Token so tests
    can verify stripping behaviour.
    """
    ing = StructuredIngredient(
        id="ing_01",
        amount=1.5,
        unit="cups",
        name="all-purpose flour",
        fallback_string="1.5 cups all-purpose flour",
        converted_amount=None,
        converted_unit=None,
        is_ai_converted=False,
    )

    if fat_tokens:
        direction_text = (
            "Whisk {{ing_01|1.5 cups all-purpose flour}} until smooth."
        )
    else:
        direction_text = "Whisk flour until smooth."

    direction = TokenizedDirection(step=1, text=direction_text)

    return IngestResponse(
        title=title,
        prep_time="10 mins",
        cook_time="20 mins",
        base_servings=4,
        source_url="https://example.com/pasta",
        image_url=None,
        categories=["Italian"],
        grid_categories={"Cuisine": ["Italian"]},
        structured_ingredients=[ing],
        tokenized_directions=[direction],
        embedding=_make_embedding(),
    )


# ---------------------------------------------------------------------------
# Test 1 — SupabaseWriter inserts all recipes
# ---------------------------------------------------------------------------

class TestSupabaseWriterInsertsAllRecipes:
    """SupabaseWriter.write() must call write_recipe_to_supabase once per recipe."""

    def test_supabase_writer_inserts_all_recipes(self, monkeypatch):
        """
        Given two IngestResponse fixtures, SupabaseWriter.write() must POST to
        /rest/v1/recipes exactly twice — once per recipe.
        """
        r1 = _make_recipe("Pasta Carbonara")
        r2 = _make_recipe("Risotto Milanese")

        # Patch the env vars so _get_creds() succeeds without a real .env
        monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")

        mock_response = MagicMock()
        mock_response.status_code = 201

        with patch("recipeparser.io.writers.supabase.httpx.post", return_value=mock_response) as mock_post:
            writer = SupabaseWriter(user_id="user-uuid-1")
            writer.write([r1, r2])

        # Filter only the /recipes calls (not /recipe_categories junction calls)
        recipes_calls = [
            c for c in mock_post.call_args_list
            if "/rest/v1/recipes" in str(c)
        ]
        assert len(recipes_calls) == 2, (
            f"Expected 2 POST calls to /rest/v1/recipes, got {len(recipes_calls)}"
        )

        # Verify the titles were sent in the correct order
        titles_sent = [
            c.kwargs["json"]["title"] if "json" in c.kwargs else c.args[1]["title"]
            for c in recipes_calls
        ]
        # Extract title from the json kwarg
        titles_sent = []
        for c in recipes_calls:
            payload = c.kwargs.get("json") or (c.args[1] if len(c.args) > 1 else {})
            titles_sent.append(payload.get("title"))

        assert "Pasta Carbonara" in titles_sent
        assert "Risotto Milanese" in titles_sent


# ---------------------------------------------------------------------------
# Test 2 — SupabaseWriter inserts recipe_categories junction rows
# ---------------------------------------------------------------------------

class TestSupabaseWriterInsertsRecipeCategories:
    """SupabaseWriter must write junction rows when category_ids are provided."""

    def test_supabase_writer_inserts_recipe_categories(self, monkeypatch):
        """
        Given a recipe with grid_categories={"Cuisine": ["Italian"]} and
        category_ids={"Italian": "cat-uuid-1"}, SupabaseWriter.write() must
        POST to /rest/v1/recipe_categories with a row containing the correct
        recipe_id, category_id, and user_id.
        """
        recipe = _make_recipe("Pasta Carbonara")

        monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")

        mock_response = MagicMock()
        mock_response.status_code = 201

        category_ids = {"Italian": "cat-uuid-1"}

        with patch("recipeparser.io.writers.supabase.httpx.post", return_value=mock_response) as mock_post:
            writer = SupabaseWriter(user_id="user-uuid-1", category_ids=category_ids)
            writer.write([recipe])

        all_calls = mock_post.call_args_list
        junction_calls = [
            c for c in all_calls
            if "/rest/v1/recipe_categories" in str(c)
        ]

        assert len(junction_calls) == 1, (
            f"Expected 1 POST to /rest/v1/recipe_categories, got {len(junction_calls)}"
        )

        # Inspect the payload
        jc = junction_calls[0]
        rows = jc.kwargs.get("json") or (jc.args[1] if len(jc.args) > 1 else [])
        assert isinstance(rows, list), "Junction payload must be a list of rows"
        assert len(rows) == 1, f"Expected 1 junction row, got {len(rows)}"

        row = rows[0]
        assert row["category_id"] == "cat-uuid-1"
        assert row["user_id"] == "user-uuid-1"
        assert "recipe_id" in row
        assert "id" in row  # PowerSync requires a UUID primary key


# ---------------------------------------------------------------------------
# Test 3 — PaprikaWriter produces a valid ZIP with Fat Tokens stripped
# ---------------------------------------------------------------------------

class TestPaprikaWriterProducesValidZip:
    """PaprikaWriter must produce a valid .paprikarecipes ZIP with no Fat Tokens."""

    def test_paprika_writer_produces_valid_zip(self, tmp_path: Path):
        """
        PaprikaWriter.write() must:
        - Produce a valid ZIP archive at the given path
        - Each entry must be a gzip-compressed JSON file
        - The JSON must have ``name`` == recipe.title
        - Fat Tokens in directions must be stripped to their fallback strings
        - The ``_cayenne_meta`` key must NOT be present
        """
        recipe = _make_recipe("Spaghetti Bolognese", fat_tokens=True)
        out_path = tmp_path / "out.paprikarecipes"

        writer = PaprikaWriter(output_path=out_path)
        writer.write([recipe])

        assert out_path.exists(), "Output archive was not created"
        assert zipfile.is_zipfile(out_path), "Output is not a valid ZIP"

        with zipfile.ZipFile(out_path, "r") as zf:
            entries = [n for n in zf.namelist() if n.endswith(".paprikarecipe")]
            assert len(entries) == 1, f"Expected 1 .paprikarecipe entry, got {len(entries)}"

            raw = zf.read(entries[0])
            data = json.loads(gzip.decompress(raw))

        # Title preserved
        assert data["name"] == "Spaghetti Bolognese"

        # Fat Tokens stripped from directions
        directions_text = data.get("directions", "")
        assert "{{" not in directions_text, (
            "Fat Token syntax must be stripped from Paprika directions"
        )
        assert "}}" not in directions_text
        # Fallback text must be present
        assert "1.5 cups all-purpose flour" in directions_text

        # No _cayenne_meta in plain Paprika export
        assert "_cayenne_meta" not in data, (
            "PaprikaWriter must NOT embed _cayenne_meta"
        )


# ---------------------------------------------------------------------------
# Test 4 — CayenneZipWriter embeds _cayenne_meta with embedding
# ---------------------------------------------------------------------------

class TestCayenneZipWriterEmbedsCayenneMeta:
    """CayenneZipWriter must embed _cayenne_meta with Fat Tokens preserved."""

    def test_cayenne_zip_writer_embeds_cayenne_meta(self, tmp_path: Path):
        """
        CayenneZipWriter.write() must:
        - Produce a valid ZIP archive
        - Each entry must contain ``_cayenne_meta``
        - ``_cayenne_meta["embedding"]`` must have length 1536
        - ``_cayenne_meta["title"]`` must equal recipe.title
        - Fat Tokens in ``_cayenne_meta["tokenized_directions"]`` must be PRESERVED
        - Plain-text ``directions`` field must have Fat Tokens stripped (Paprika compat)
        """
        recipe = _make_recipe("Chicken Tikka Masala", fat_tokens=True)
        out_path = tmp_path / "cayenne_out.paprikarecipes"

        writer = CayenneZipWriter(output_path=out_path)
        writer.write([recipe])

        assert out_path.exists(), "Output archive was not created"
        assert zipfile.is_zipfile(out_path), "Output is not a valid ZIP"

        with zipfile.ZipFile(out_path, "r") as zf:
            entries = [n for n in zf.namelist() if n.endswith(".paprikarecipe")]
            assert len(entries) == 1

            raw = zf.read(entries[0])
            data = json.loads(gzip.decompress(raw))

        # _cayenne_meta must be present
        assert "_cayenne_meta" in data, "CayenneZipWriter must embed _cayenne_meta"

        meta = data["_cayenne_meta"]

        # Title preserved in meta
        assert meta["title"] == "Chicken Tikka Masala"

        # Embedding must be 1536-dim
        assert "embedding" in meta, "_cayenne_meta must contain 'embedding'"
        assert len(meta["embedding"]) == _EMBEDDING_DIM, (
            f"Embedding must be {_EMBEDDING_DIM}-dim, got {len(meta['embedding'])}"
        )

        # Fat Tokens PRESERVED in meta tokenized_directions
        meta_directions = meta.get("tokenized_directions", [])
        assert len(meta_directions) == 1
        assert "{{" in meta_directions[0]["text"], (
            "Fat Tokens must be PRESERVED in _cayenne_meta tokenized_directions"
        )

        # Plain-text directions field must have Fat Tokens stripped
        plain_directions = data.get("directions", "")
        assert "{{" not in plain_directions, (
            "Fat Tokens must be stripped from the plain-text directions field"
        )
        assert "1.5 cups all-purpose flour" in plain_directions


# ---------------------------------------------------------------------------
# Test 5 — Round-trip: CayenneZipWriter → PaprikaReader → Flow B (zero cost)
# ---------------------------------------------------------------------------

class TestRoundTripCayenneZipToPaprikaReaderIsZeroCost:
    """
    A CayenneZipWriter archive read back by PaprikaReader must route every
    entry to Flow B (InputType.PAPRIKA_CAYENNE) with no text payload.
    """

    def test_round_trip_cayenne_zip_to_paprika_reader_is_zero_cost(self, tmp_path: Path):
        """
        Round-trip invariants:
        - All chunks have input_type == InputType.PAPRIKA_CAYENNE
        - chunk.pre_parsed.title == recipe.title
        - chunk.pre_parsed_embedding has length 1536
        - chunk.text == "" (no text needed for Flow B ASSEMBLE stage)
        """
        recipe = _make_recipe("Beef Wellington", fat_tokens=True)
        out_path = tmp_path / "roundtrip.paprikarecipes"

        # Write
        CayenneZipWriter(output_path=out_path).write([recipe])

        # Read back
        reader = PaprikaReader()
        chunks = reader.read(str(out_path))

        assert len(chunks) == 1, f"Expected 1 chunk, got {len(chunks)}"

        chunk = chunks[0]

        # Flow B routing
        assert chunk.input_type == InputType.PAPRIKA_CAYENNE, (
            f"Expected PAPRIKA_CAYENNE, got {chunk.input_type!r}"
        )

        # pre_parsed must be a CayenneRecipe with the correct title
        assert chunk.pre_parsed is not None, "chunk.pre_parsed must not be None for Flow B"
        assert chunk.pre_parsed.title == "Beef Wellington"

        # Embedding must be carried through
        assert chunk.pre_parsed_embedding is not None, (
            "chunk.pre_parsed_embedding must not be None for Flow B"
        )
        assert len(chunk.pre_parsed_embedding) == _EMBEDDING_DIM, (
            f"Embedding must be {_EMBEDDING_DIM}-dim, got {len(chunk.pre_parsed_embedding)}"
        )

        # No text payload — Flow B goes straight to ASSEMBLE, no Gemini calls
        assert chunk.text == "", (
            f"Flow B chunk.text must be empty string, got {chunk.text!r}"
        )
