"""
Phase 3 gate tests — Reader contracts.

Tests verify that every RecipeReader implementation returns the correct
Chunk shape and InputType for each source kind.

Gate command: pytest tests/unit/readers/ -v
"""

from __future__ import annotations

import gzip
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.core.models import Chunk, InputType
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.readers.url import UrlReader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_paprikarecipes(entries: List[Dict[str, Any]]) -> str:
    """
    Build a temporary .paprikarecipes ZIP archive from a list of recipe dicts.

    Each dict is gzip-compressed and stored as a .paprikarecipe entry inside
    the ZIP.  Returns the path to the temporary file (caller must delete it).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, entry in enumerate(entries):
            compressed = gzip.compress(json.dumps(entry).encode())
            zf.writestr(f"recipe_{i}.paprikarecipe", compressed)
    buf.seek(0)

    tmp = tempfile.NamedTemporaryFile(
        suffix=".paprikarecipes", delete=False
    )
    tmp.write(buf.read())
    tmp.close()
    return tmp.name


def _write_paprika_archive(tmp_path: Path, entries: List[Dict[str, Any]]) -> Path:
    """
    Build a .paprikarecipes archive under ``tmp_path`` from a list of recipe dicts.

    Each dict is gzip-compressed and stored as a .paprikarecipe entry inside
    the ZIP, matching what PaprikaReader.read_entries expects.
    """
    archive = tmp_path / "export.paprikarecipes"
    with zipfile.ZipFile(archive, "w") as zf:
        for i, entry in enumerate(entries):
            compressed = gzip.compress(json.dumps(entry).encode())
            zf.writestr(f"recipe_{i}.paprikarecipe", compressed)
    return archive


# Minimal valid IngestResponse payload (matches recipeparser/models.py)
_INGEST_RESPONSE_PAYLOAD: Dict[str, Any] = {
    "title": "Test Cake",
    "prep_time": None,
    "cook_time": None,
    "base_servings": 4.0,
    "source_url": None,
    "image_url": None,
    "categories": [],
    "grid_categories": {},
    "structured_ingredients": [
        {
            "id": "ing_01",
            "amount": 1.5,
            "unit": "cups",
            "name": "flour",
            "fallback_string": "1.5 cups flour",
            "converted_amount": None,
            "converted_unit": None,
            "is_ai_converted": False,
        }
    ],
    "tokenized_directions": [
        {"step": 1, "text": "Mix {{ing_01|1.5 cups flour}}."}
    ],
    "embedding": [0.1] * 1536,
}


# ---------------------------------------------------------------------------
# UrlReader tests
# ---------------------------------------------------------------------------


def test_url_reader_returns_single_chunk_with_url_input_type() -> None:
    """
    UrlReader.read() must return exactly one Chunk with:
    - input_type == InputType.URL
    - source_url == the original URL (not the Jina-prefixed one)
    - text == the body returned by requests.get
    """
    fake_body = "# Chocolate Cake\n\nIngredients: flour, sugar, cocoa"
    mock_response = MagicMock()
    mock_response.text = fake_body
    mock_response.raise_for_status = MagicMock()

    with patch("recipeparser.io.readers.url.requests.get", return_value=mock_response) as mock_get:
        reader = UrlReader()
        source = "https://example.com/chocolate-cake"
        chunks = reader.read(source)

    # Verify Jina prefix was applied
    mock_get.assert_called_once_with(
        f"https://r.jina.ai/{source}", timeout=30
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert isinstance(chunk, Chunk)
    assert chunk.input_type == InputType.URL
    assert chunk.source_url == source
    assert chunk.text == fake_body


# ---------------------------------------------------------------------------
# PaprikaReader — legacy entry tests
# ---------------------------------------------------------------------------


def test_paprika_reader_legacy_entry_returns_paprika_legacy_type() -> None:
    """
    A .paprikarecipes entry WITHOUT ``_cayenne_meta`` must produce a Chunk with:
    - input_type == InputType.PAPRIKA_LEGACY
    - text containing the recipe name, ingredients, and directions
    - pre_parsed is None
    - pre_parsed_embedding is None
    """
    entry = {
        "name": "Grandma's Cookies",
        "ingredients": "2 cups flour\n1 cup sugar",
        "directions": "Mix and bake at 350°F for 12 minutes.",
    }
    archive_path = _make_paprikarecipes([entry])

    try:
        reader = PaprikaReader()
        chunks = reader.read(archive_path)
    finally:
        os.unlink(archive_path)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert isinstance(chunk, Chunk)
    assert chunk.input_type == InputType.PAPRIKA_LEGACY
    assert "Grandma's Cookies" in chunk.text
    assert "flour" in chunk.text
    assert "bake" in chunk.text
    assert chunk.pre_parsed is None
    assert chunk.pre_parsed_embedding is None


# ---------------------------------------------------------------------------
# PaprikaReader — Cayenne entry WITH embedding
# ---------------------------------------------------------------------------


def test_paprika_reader_cayenne_entry_with_embedding_returns_cayenne_type() -> None:
    """
    A .paprikarecipes entry WITH ``_cayenne_meta`` that includes an embedding must
    produce a Chunk with:
    - input_type == InputType.PAPRIKA_CAYENNE
    - pre_parsed is a CayenneRecipe instance (embedding is stored separately)
    - pre_parsed_embedding is a list of 1536 floats
    - text is empty (fast-path; no extraction needed)
    """
    from recipeparser.models import CayenneRecipe

    meta = dict(_INGEST_RESPONSE_PAYLOAD)  # includes "embedding"
    entry = {
        "name": "Test Cake",
        "_cayenne_meta": meta,
    }
    archive_path = _make_paprikarecipes([entry])

    try:
        reader = PaprikaReader()
        chunks = reader.read(archive_path)
    finally:
        os.unlink(archive_path)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert isinstance(chunk, Chunk)
    assert chunk.input_type == InputType.PAPRIKA_CAYENNE
    assert chunk.text == ""
    assert isinstance(chunk.pre_parsed, CayenneRecipe)
    assert chunk.pre_parsed.title == "Test Cake"
    assert isinstance(chunk.pre_parsed_embedding, list)
    assert len(chunk.pre_parsed_embedding) == 1536
    assert chunk.pre_parsed_embedding[0] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# PaprikaReader — Cayenne entry WITHOUT embedding
# ---------------------------------------------------------------------------


def test_paprika_reader_cayenne_entry_without_embedding_returns_cayenne_type_no_embedding() -> None:
    """
    A .paprikarecipes entry WITH ``_cayenne_meta`` but WITHOUT an embedding key
    must produce a Chunk with:
    - input_type == InputType.PAPRIKA_CAYENNE
    - pre_parsed is a CayenneRecipe instance (embedding is stored separately)
    - pre_parsed_embedding is None  (triggers EMBED stage in the pipeline)
    """
    from recipeparser.models import CayenneRecipe

    meta = {k: v for k, v in _INGEST_RESPONSE_PAYLOAD.items() if k != "embedding"}
    entry = {
        "name": "Test Cake",
        "_cayenne_meta": meta,
    }
    archive_path = _make_paprikarecipes([entry])

    try:
        reader = PaprikaReader()
        chunks = reader.read(archive_path)
    finally:
        os.unlink(archive_path)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert isinstance(chunk, Chunk)
    assert chunk.input_type == InputType.PAPRIKA_CAYENNE
    assert isinstance(chunk.pre_parsed, CayenneRecipe)
    assert chunk.pre_parsed_embedding is None


# ---------------------------------------------------------------------------
# PaprikaReader — corrupt _cayenne_meta falls back to legacy
# ---------------------------------------------------------------------------


def test_paprika_reader_corrupt_cayenne_meta_falls_back_to_legacy() -> None:
    """
    If _cayenne_meta is present but CayenneRecipe validation fails, the reader
    must NOT emit PAPRIKA_CAYENNE with pre_parsed=None. It should fall back to
    Flow A using the Paprika name/ingredients/directions fields.
    """
    entry = {
        "name": "Broken Meta Cake",
        "ingredients": "1 cup sugar",
        "directions": "Bake.",
        # Invalid CayenneRecipe: title must be str, not int
        "_cayenne_meta": {"title": 999},
    }
    archive_path = _make_paprikarecipes([entry])

    try:
        reader = PaprikaReader()
        chunks = reader.read(archive_path)
    finally:
        os.unlink(archive_path)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.input_type == InputType.PAPRIKA_LEGACY
    assert "Broken Meta Cake" in chunk.text
    assert "sugar" in chunk.text
    assert chunk.pre_parsed is None
    assert chunk.pre_parsed_embedding is None


# ---------------------------------------------------------------------------
# PaprikaReader — chunk labeling
# ---------------------------------------------------------------------------


def test_paprika_reader_labels_each_chunk_with_the_entry_name(tmp_path):
    """Spec 4.2: a dropped chunk must be nameable, and Paprika supplies real names."""
    archive = _write_paprika_archive(
        tmp_path,
        [{"name": "Sticky Toffee Pudding", "ingredients": "1 cup dates", "directions": "Bake."}],
    )

    chunks = PaprikaReader().read(str(archive))

    assert [c.label for c in chunks] == ["Sticky Toffee Pudding"]


# ---------------------------------------------------------------------------
# PaprikaReader — photo_data decoding (Task 4)
# ---------------------------------------------------------------------------


def test_paprika_photo_data_is_decoded_to_bytes(tmp_path):
    """photo_data is base64 text; image_bytes is typed bytes. Uploading the string
    unchanged would store base64 as a JPEG."""
    import base64

    jpeg = b"\xff\xd8\xff\xe0 not really a jpeg but it is bytes"
    archive = _write_paprika_archive(
        tmp_path,
        [{
            "name": "Photographed Thing",
            "ingredients": "1 cup x",
            "directions": "Cook.",
            "photo": "pg_1.jpg",
            "photo_data": base64.b64encode(jpeg).decode("ascii"),
        }],
    )

    chunks = PaprikaReader().read(str(archive))

    assert chunks[0].image_bytes == jpeg
    assert chunks[0].image_content_type == "image/jpeg"


def test_paprika_png_photo_extension_maps_to_png_content_type(tmp_path):
    """image/jpeg is also _PHOTO_TYPES's fallback for an unrecognised extension,
    so a .jpg-only test cannot tell "derived from the filename" from "took the
    default". A .png entry must map to image/png, not silently fall back."""
    import base64

    png = b"\x89PNG\r\n\x1a\n not really a png but it is bytes"
    archive = _write_paprika_archive(
        tmp_path,
        [{
            "name": "Photographed Thing",
            "ingredients": "1 cup x",
            "directions": "Cook.",
            "photo": "pg_1.png",
            "photo_data": base64.b64encode(png).decode("ascii"),
        }],
    )

    chunks = PaprikaReader().read(str(archive))

    assert chunks[0].image_bytes == png
    assert chunks[0].image_content_type == "image/png"


def test_line_wrapped_photo_data_is_still_decoded(tmp_path):
    """Some exporters wrap base64 at 76 columns (MIME-style, RFC 2045); a photo
    encoded that way must not be dropped as undecodable."""
    import base64

    jpeg = bytes(range(256)) * 4  # long enough that encodebytes actually wraps
    wrapped = base64.encodebytes(jpeg).decode("ascii")
    assert "\n" in wrapped  # sanity: the fixture really does wrap

    archive = _write_paprika_archive(
        tmp_path,
        [{
            "name": "Wrapped Photo",
            "ingredients": "1 cup x",
            "directions": "Cook.",
            "photo": "pg_1.jpg",
            "photo_data": wrapped,
        }],
    )

    chunks = PaprikaReader().read(str(archive))

    assert chunks[0].image_bytes == jpeg
    assert chunks[0].image_content_type == "image/jpeg"


def test_unreadable_photo_data_is_dropped_not_raised(tmp_path):
    archive = _write_paprika_archive(
        tmp_path,
        [{"name": "Bad Photo", "ingredients": "x", "directions": "y", "photo_data": "!!!not base64!!!"}],
    )

    chunks = PaprikaReader().read(str(archive))

    assert chunks[0].image_bytes is None


class TestPaprikaLegacyMetadata:
    """Design §5.2 — what a legacy entry carries through to the chunk."""

    def test_a_full_entry_yields_meta_source_url_and_a_servings_line(self, tmp_path: Path):
        entry = {
            "name": "Chicken Pie",
            "ingredients": "1 chicken",
            "directions": "Bake it.",
            "prep_time": "20 min",
            "cook_time": "1 hr",
            "servings": "Serves 4 to 6",
            "source_url": "https://example.com/pie",
            "image_url": "https://example.com/pie.jpg",
        }
        archive = _write_paprika_archive(tmp_path, [entry])

        chunk = PaprikaReader().read(str(archive))[0]

        assert chunk.input_type == InputType.PAPRIKA_LEGACY
        assert chunk.source_url == "https://example.com/pie"
        assert chunk.image_url == "https://example.com/pie.jpg"
        assert chunk.meta is not None
        assert chunk.meta.prep_time == "20 min"
        assert chunk.meta.cook_time == "1 hr"
        # The refine stage reads servings out of the text; no parser lives here (P4).
        assert "Servings: Serves 4 to 6" in chunk.text
        assert chunk.text.index("Servings:") < chunk.text.index("Ingredients:")
        assert chunk.text.index("Chicken Pie") < chunk.text.index("Servings:")

    def test_empty_strings_and_a_zero_rating_come_through_as_none(self, tmp_path: Path):
        entry = {
            "name": "Plain",
            "ingredients": "x",
            "directions": "y",
            "prep_time": "",
            "cook_time": "   ",
            "servings": "",
            "source_url": "",
            "image_url": "",
            "rating": 0,
        }
        archive = _write_paprika_archive(tmp_path, [entry])

        chunk = PaprikaReader().read(str(archive))[0]

        assert chunk.source_url is None
        assert chunk.image_url is None
        assert chunk.meta is not None
        assert chunk.meta.prep_time is None
        assert chunk.meta.cook_time is None
        assert chunk.meta.rating is None
        assert "Servings:" not in chunk.text

    def test_an_embedded_photo_wins_over_the_image_url(self, tmp_path: Path):
        # The pipeline uploads bytes in preference; a web address left beside them
        # would outlive the upload and point at the wrong picture.
        import base64

        entry = {
            "name": "Photographed",
            "ingredients": "x",
            "directions": "y",
            "photo": "hero.png",
            "photo_data": base64.b64encode(b"not-really-a-png").decode(),
            "image_url": "https://example.com/stale.jpg",
        }
        archive = _write_paprika_archive(tmp_path, [entry])

        chunk = PaprikaReader().read(str(archive))[0]

        assert chunk.image_bytes == b"not-really-a-png"
        assert chunk.image_content_type == "image/png"
        assert chunk.image_url is None

    def test_a_cayenne_entry_is_untouched_by_any_of_this(self, tmp_path: Path):
        entry = dict(_INGEST_RESPONSE_PAYLOAD)
        archive = _write_paprika_archive(
            tmp_path, [{"name": "Test Cake", "_cayenne_meta": entry}]
        )

        chunk = PaprikaReader().read(str(archive))[0]

        assert chunk.input_type == InputType.PAPRIKA_CAYENNE
        assert chunk.meta is None
