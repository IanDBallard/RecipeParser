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
