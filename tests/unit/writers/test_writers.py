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
from recipeparser.io.writers.image_store import SupabaseImageStore
from recipeparser.io.writers.paprika_zip import PaprikaWriter
from recipeparser.io.writers.supabase import SupabaseWriter, write_recipe_to_supabase
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
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
        # httpx.post is mocked below, so no network call ever leaves this
        # process — the live-write guard exists to stop a *real* Supabase
        # write from a pytest run, which this isn't.
        monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")

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
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
        # httpx.post is mocked below, so no network call ever leaves this
        # process — the live-write guard exists to stop a *real* Supabase
        # write from a pytest run, which this isn't.
        monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")

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


# ---------------------------------------------------------------------------
# Test 6 — SupabaseImageStore (Task 4)
# ---------------------------------------------------------------------------


def test_image_store_returns_none_without_credentials(monkeypatch):
    """No credentials is a recipe without a picture, never a raised exception."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    assert SupabaseImageStore().put(b"bytes", "some-id") is None


def test_image_store_returns_none_for_empty_bytes():
    assert SupabaseImageStore(url="https://x.test", service_key="k").put(b"", "some-id") is None


def test_image_store_uploads_and_returns_public_url():
    """Happy path: neither of the two tests above reaches this far. Asserts
    the object path built from bucket + recipe_id + extension, the
    content-type/upsert options passed to the client, and that put() returns
    the public URL the client hands back.

    supabase.create_client is imported lazily inside put(), so it is patched
    where it is looked up: the `supabase` module's own attribute.
    """
    mock_client = MagicMock()
    mock_client.storage.from_.return_value.get_public_url.return_value = (
        "https://fake.supabase.co/storage/v1/object/public/recipe-images/some-id.jpg"
    )

    with patch("supabase.create_client", return_value=mock_client) as mock_create:
        store = SupabaseImageStore(url="https://fake.supabase.co", service_key="fake-service-key")
        result = store.put(b"bytes", "some-id", "image/jpeg")

    mock_create.assert_called_once_with("https://fake.supabase.co", "fake-service-key")
    mock_client.storage.from_.assert_any_call("recipe-images")

    upload_call = mock_client.storage.from_.return_value.upload.call_args
    assert upload_call.args[0] == "recipe-images/some-id.jpg"
    assert upload_call.args[1] == b"bytes"
    assert upload_call.args[2] == {"content-type": "image/jpeg", "upsert": "true"}

    assert result == "https://fake.supabase.co/storage/v1/object/public/recipe-images/some-id.jpg"


def test_image_store_maps_a_non_jpeg_content_type_to_its_extension():
    """.jpg is also _EXTENSIONS's fallback for an unrecognised content type, so
    a .jpg-only test cannot tell "derived from content_type" from "took the
    default". A image/png content type must produce a .png object path and
    be passed through to the client unchanged."""
    mock_client = MagicMock()
    mock_client.storage.from_.return_value.get_public_url.return_value = (
        "https://fake.supabase.co/storage/v1/object/public/recipe-images/some-id.png"
    )

    with patch("supabase.create_client", return_value=mock_client):
        store = SupabaseImageStore(url="https://fake.supabase.co", service_key="fake-service-key")
        result = store.put(b"bytes", "some-id", "image/png")

    upload_call = mock_client.storage.from_.return_value.upload.call_args
    assert upload_call.args[0] == "recipe-images/some-id.png"
    assert upload_call.args[2] == {"content-type": "image/png", "upsert": "true"}

    assert result == "https://fake.supabase.co/storage/v1/object/public/recipe-images/some-id.png"


# ---------------------------------------------------------------------------
# Test 7 — Unquantified ingredient (Task 9)
# ---------------------------------------------------------------------------


def test_an_unquantified_ingredient_serialises_as_null():
    """Spec 4.8: 'to taste' must not be indistinguishable from a real zero."""
    ingredient = StructuredIngredient(
        id="ing_01",
        amount=None,
        unit=None,
        name="Kosher salt",
        fallback_string="Kosher salt",
        converted_amount=None,
        converted_unit=None,
        is_ai_converted=False,
    )

    dumped = ingredient.model_dump()

    assert dumped["amount"] is None
    assert '"amount": null' in json.dumps(dumped)


# ---------------------------------------------------------------------------
# jsonb columns must reach Postgres as arrays, not as strings
# ---------------------------------------------------------------------------

def test_the_jsonb_columns_are_sent_as_arrays_not_strings(monkeypatch):
    """A jsonb column handed a JSON *string* stores a JSON string, not an array.

    Every row in the live library was written that way: on 2026-09-06
    jsonb_typeof(structured_ingredients) reported 'string' for all 786 rows,
    with no arrays in the table at all. Nothing looked broken because the
    Cayenne client compensates — kitchenRecipe.ts's parseArrayColumn parses,
    sees a string, and parses again — so this survived unnoticed and every
    import kept adding to it.

    PostgREST accepts a real list for a jsonb column, exactly as it already
    does for the pgvector `embedding` two lines below in the same payload.
    """
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
    # httpx.post is mocked, so nothing leaves this process.
    monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")

    mock_response = MagicMock()
    mock_response.status_code = 201

    with patch("recipeparser.io.writers.supabase.httpx.post", return_value=mock_response) as mock_post:
        write_recipe_to_supabase(_make_recipe("Pasta Carbonara"), "user-uuid-1")

    payload = next(
        c.kwargs["json"] for c in mock_post.call_args_list
        if "/rest/v1/recipes" in str(c)
    )

    assert isinstance(payload["structured_ingredients"], list), (
        "structured_ingredients was sent as "
        f"{type(payload['structured_ingredients']).__name__}, which Postgres stores as a JSON string"
    )
    assert isinstance(payload["tokenized_directions"], list), (
        "tokenized_directions was sent as "
        f"{type(payload['tokenized_directions']).__name__}, which Postgres stores as a JSON string"
    )
    # Elements survive as objects, not as re-encoded text.
    assert payload["structured_ingredients"][0]["name"] == "all-purpose flour"
    assert payload["tokenized_directions"][0]["step"] == 1


# ---------------------------------------------------------------------------
# The six Paprika metadata columns (design 6.3)
# ---------------------------------------------------------------------------

#: A recipe that states all six. model_copy rather than attribute assignment,
#: matching how this suite already builds variants of the shared fixture.
_RATED = {
    "source": "Bon Appetit",
    "notes": "Chill the dough.",
    "rating": 4,
    "nutritional_info": "520 kcal",
    "description": "A cold-weather pie.",
    "difficulty": "Moderate",
}


def _recipes_payload(mock_post):
    """The row posted to /rest/v1/recipes, ignoring the category junction calls."""
    return next(
        c.kwargs["json"] for c in mock_post.call_args_list
        if "/rest/v1/recipes" in str(c)
    )


def _fake_supabase(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
    # httpx.post is mocked by the caller, so nothing leaves this process.
    monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")
    response = MagicMock()
    response.status_code = 201
    return response


def test_the_supabase_row_carries_all_six(monkeypatch):
    response = _fake_supabase(monkeypatch)

    with patch("recipeparser.io.writers.supabase.httpx.post", return_value=response) as mock_post:
        write_recipe_to_supabase(
            _make_recipe("Chicken Pie").model_copy(update=_RATED), "user-uuid-1"
        )

    payload = _recipes_payload(mock_post)
    assert payload["source"] == "Bon Appetit"
    assert payload["notes"] == "Chill the dough."
    assert payload["rating"] == 4
    assert payload["nutritional_info"] == "520 kcal"
    assert payload["description"] == "A cold-weather pie."
    assert payload["difficulty"] == "Moderate"


def test_an_unrated_recipe_writes_null_not_zero(monkeypatch):
    """The column's check constraint rejects 0; unrated is null."""
    response = _fake_supabase(monkeypatch)

    with patch("recipeparser.io.writers.supabase.httpx.post", return_value=response) as mock_post:
        write_recipe_to_supabase(_make_recipe("Plain"), "user-uuid-1")

    assert _recipes_payload(mock_post)["rating"] is None


def _read_one_entry(archive: Path) -> dict:
    """Decompress the single .paprikarecipe entry in a written archive."""
    with zipfile.ZipFile(archive, "r") as zf:
        entries = [n for n in zf.namelist() if n.endswith(".paprikarecipe")]
        assert len(entries) == 1, f"expected one entry, found {entries}"
        return json.loads(gzip.decompress(zf.read(entries[0])))


def test_the_paprika_writer_carries_the_six(tmp_path: Path):
    out = tmp_path / "export.paprikarecipes"
    PaprikaWriter(out).write([_make_recipe("Chicken Pie").model_copy(update=_RATED)])

    entry = _read_one_entry(out)

    assert entry["source"] == "Bon Appetit"
    assert entry["notes"] == "Chill the dough."
    assert entry["rating"] == 4
    assert entry["nutritional_info"] == "520 kcal"
    assert entry["description"] == "A cold-weather pie."
    assert entry["difficulty"] == "Moderate"


def test_the_cayenne_writer_carries_them_at_both_levels(tmp_path: Path):
    out = tmp_path / "export.paprikarecipes"
    CayenneZipWriter(out).write([_make_recipe("Chicken Pie").model_copy(update=_RATED)])

    entry = _read_one_entry(out)

    assert entry["source"] == "Bon Appetit"
    assert entry["rating"] == 4
    assert entry["_cayenne_meta"]["source"] == "Bon Appetit"
    assert entry["_cayenne_meta"]["rating"] == 4
    assert entry["_cayenne_meta"]["difficulty"] == "Moderate"


def test_an_unrated_recipe_writes_zero_at_the_paprika_level(tmp_path: Path):
    """Paprika's rating is an integer; the null lives in _cayenne_meta instead."""
    out = tmp_path / "export.paprikarecipes"
    CayenneZipWriter(out).write([_make_recipe("Plain")])

    entry = _read_one_entry(out)

    assert entry["rating"] == 0
    assert entry["_cayenne_meta"]["rating"] is None


def test_an_absent_text_field_writes_an_empty_string(tmp_path: Path):
    out = tmp_path / "export.paprikarecipes"
    PaprikaWriter(out).write([_make_recipe("Plain")])

    entry = _read_one_entry(out)

    assert entry["source"] == ""
    assert entry["notes"] == ""


def test_a_cayenne_archive_round_trips_every_new_field(tmp_path: Path):
    """Design 6.4 - write, read back, and the six survive with no Gemini call."""
    out = tmp_path / "export.paprikarecipes"
    CayenneZipWriter(out).write([_make_recipe("Chicken Pie").model_copy(update=_RATED)])

    chunk = PaprikaReader().read(str(out))[0]

    assert chunk.input_type == InputType.PAPRIKA_CAYENNE
    assert chunk.pre_parsed is not None
    assert chunk.pre_parsed.source == "Bon Appetit"
    assert chunk.pre_parsed.notes == "Chill the dough."
    assert chunk.pre_parsed.rating == 4
    assert chunk.pre_parsed.nutritional_info == "520 kcal"
    assert chunk.pre_parsed.description == "A cold-weather pie."
    assert chunk.pre_parsed.difficulty == "Moderate"
