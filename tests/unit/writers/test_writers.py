"""
tests/unit/writers/test_writers.py — Phase 5 gate tests for RecipeWriter implementations.

Gate command: pytest tests/unit/writers/ -v

Five tests at first. Test 1 checked the SupabaseWriter class's loop and went
with the class, which nothing but tests constructed (Fix Roadmap F-068):
  2. test_the_writer_inserts_recipe_categories (write_recipe_to_supabase)
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
from recipeparser.io.writers.supabase import write_recipe_to_supabase
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
# Test 2 — the Supabase writer inserts recipe_categories junction rows
# ---------------------------------------------------------------------------

class TestTheSupabaseWriterInsertsRecipeCategories:
    """write_recipe_to_supabase must write junction rows when category_ids are provided."""

    def test_the_writer_inserts_recipe_categories(self, monkeypatch):
        """
        Given a recipe with grid_categories={"Cuisine": ["Italian"]} and
        category_ids={"Italian": "cat-uuid-1"}, write_recipe_to_supabase() must
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
            write_recipe_to_supabase(recipe, "user-uuid-1", category_ids=category_ids)

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

    def test_one_refused_category_does_not_lose_the_others(self, monkeypatch):
        """
        Fix Roadmap F-004. The junction rows go up in one request, so a category
        deleted mid-import makes the foreign key refuse the whole batch — and the
        recipe lost EVERY category link, not just the one that had gone. The
        writer must retry row by row so the surviving links still land.
        """
        recipe = _make_recipe("Pad Thai")
        recipe.grid_categories = {"Cuisine": ["Thai"], "Speed": ["Quick"]}
        monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
        # httpx.post is mocked below; nothing leaves this process.
        monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")

        def post(url, *, headers, json, timeout, params=None):
            resp = MagicMock()
            if url.endswith("/recipe_categories") and any(
                r["category_id"] == "cat-gone" for r in json
            ):
                resp.status_code = 409     # 23503: the category was deleted
                resp.text = 'insert or update on table "recipe_categories" violates foreign key'
            else:
                resp.status_code = 201
            return resp

        with patch("recipeparser.io.writers.supabase.httpx.post", side_effect=post) as mock_post:
            write_recipe_to_supabase(
                recipe, "user-uuid-1",
                category_ids={"Thai": "cat-thai", "Quick": "cat-gone"},
            )

        landed = [
            r["category_id"]
            for c in mock_post.call_args_list
            if c.args[0].endswith("/recipe_categories")
            and not any(r["category_id"] == "cat-gone" for r in c.kwargs["json"])
            for r in c.kwargs["json"]
        ]
        assert landed == ["cat-thai"], (
            "the link to the surviving category must still be written after the "
            f"batch is refused; landed={landed}"
        )

    def test_a_refused_link_is_reported_to_the_caller(self, monkeypatch):
        """
        Fix Roadmap F-115. F-004's retry kept the surviving links but only logged
        the refused one, so the job that wrote the recipe never heard about it.
        The writer hands each link it could not write to the caller, with why.
        """
        recipe = _make_recipe("Pad Thai")
        recipe.grid_categories = {"Cuisine": ["Thai"], "Speed": ["Quick"]}
        monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
        # httpx.post is mocked below; nothing leaves this process.
        monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")

        def post(url, *, headers, json, timeout, params=None):
            resp = MagicMock()
            if url.endswith("/recipe_categories") and any(
                r["category_id"] == "cat-gone" for r in json
            ):
                resp.status_code = 409
                resp.text = 'violates foreign key constraint "recipe_categories_category_id_fkey"'
            else:
                resp.status_code = 201
            return resp

        refused: List[dict] = []
        with patch("recipeparser.io.writers.supabase.httpx.post", side_effect=post):
            write_recipe_to_supabase(
                recipe, "user-uuid-1",
                category_ids={"Thai": "cat-thai", "Quick": "cat-gone"},
                on_link_refused=refused.append,
            )

        assert [r["category_id"] for r in refused] == ["cat-gone"]
        assert "409" in refused[0]["reason"] and "foreign key" in refused[0]["reason"]

    def test_links_lost_to_a_network_error_are_reported_to_the_caller(self, monkeypatch):
        """F-115: a batch that never got an answer lost every link it carried."""
        import httpx

        recipe = _make_recipe("Pad Thai")
        recipe.grid_categories = {"Cuisine": ["Thai"], "Speed": ["Quick"]}
        monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
        # httpx.post is mocked below; nothing leaves this process.
        monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")

        def post(url, *, headers, json, timeout, params=None):
            if url.endswith("/recipe_categories"):
                raise httpx.ConnectError("connection reset")
            resp = MagicMock()
            resp.status_code = 201
            return resp

        refused: List[dict] = []
        with patch("recipeparser.io.writers.supabase.httpx.post", side_effect=post):
            write_recipe_to_supabase(
                recipe, "user-uuid-1",
                category_ids={"Thai": "cat-thai", "Quick": "cat-quick"},
                on_link_refused=refused.append,
            )

        assert sorted(r["category_id"] for r in refused) == ["cat-quick", "cat-thai"]
        assert all("connection reset" in r["reason"] for r in refused)

    def test_a_network_error_on_the_batch_is_retried_row_by_row(self, monkeypatch):
        """
        Fix Roadmap F-128. A refused batch was retried row by row, but a batch
        that got no answer was not retried at all, so one dropped connection
        lost every link. The insert ignores duplicates, so the rows are safe to
        send again even if the batch did land.
        """
        import httpx

        recipe = _make_recipe("Pad Thai")
        recipe.grid_categories = {"Cuisine": ["Thai"], "Speed": ["Quick"]}
        monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
        # httpx.post is mocked below; nothing leaves this process.
        monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")

        junction_calls: List[list] = []

        def post(url, *, headers, json, timeout, params=None):
            if url.endswith("/recipe_categories"):
                junction_calls.append(json)
                if len(junction_calls) == 1:
                    raise httpx.ConnectError("connection reset")
            resp = MagicMock()
            resp.status_code = 201
            return resp

        refused: List[dict] = []
        with patch("recipeparser.io.writers.supabase.httpx.post", side_effect=post):
            write_recipe_to_supabase(
                recipe, "user-uuid-1",
                category_ids={"Thai": "cat-thai", "Quick": "cat-quick"},
                on_link_refused=refused.append,
            )

        assert refused == []
        assert [len(rows) for rows in junction_calls] == [2, 1, 1]

    def test_the_junction_insert_ignores_duplicates(self, monkeypatch):
        """F-128's retry is only safe while the insert ignores a link that already landed."""
        recipe = _make_recipe("Pad Thai")
        recipe.grid_categories = {"Cuisine": ["Thai"]}
        monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-key")
        monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")
        resp = MagicMock()
        resp.status_code = 201
        with patch("recipeparser.io.writers.supabase.httpx.post", return_value=resp) as mock_post:
            write_recipe_to_supabase(recipe, "user-uuid-1", category_ids={"Thai": "cat-thai"})
        junction = [c for c in mock_post.call_args_list if c.args[0].endswith("/recipe_categories")]
        assert "resolution=ignore-duplicates" in junction[0].kwargs["headers"]["Prefer"]
        # Each row carries a fresh id, so without a conflict target PostgREST arbitrates on the
        # primary key, which never fires, and a link that landed is refused with a 409.
        assert junction[0].kwargs["params"] == {"on_conflict": "recipe_id,category_id"}


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
    the object key built from recipe_id + extension -- from_(BUCKET) already
    scopes the upload to the bucket, so repeating it in the key is what put a
    literal recipe-images/recipe-images/ prefix on the one object stored before
    this was fixed. Also asserts the
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
    assert upload_call.args[0] == "some-id.jpg"
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
    assert upload_call.args[0] == "some-id.png"
    assert upload_call.args[2] == {"content-type": "image/png", "upsert": "true"}

    assert result == "https://fake.supabase.co/storage/v1/object/public/recipe-images/some-id.png"


def test_image_store_stores_a_large_picture_scaled_as_a_jpeg():
    """Every picture goes through put(), so it is where a Paprika photo, an imported photo and a
    page's hero image are all brought to Cayenne's 1600 px edge; a re-encoded PNG is a .jpg."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4032, 3024), (1, 2, 3)).save(buf, "PNG")
    mock_client = MagicMock()
    mock_client.storage.from_.return_value.get_public_url.return_value = "https://x/some-id.jpg"

    with patch("supabase.create_client", return_value=mock_client):
        SupabaseImageStore(url="https://fake.supabase.co", service_key="k").put(buf.getvalue(), "some-id", "image/png")

    upload_call = mock_client.storage.from_.return_value.upload.call_args
    assert upload_call.args[0] == "some-id.jpg"
    assert upload_call.args[2] == {"content-type": "image/jpeg", "upsert": "true"}
    assert Image.open(io.BytesIO(upload_call.args[1])).size == (1600, 1200)


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
