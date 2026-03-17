"""Tests for the Cayenne Ingestion API (/ingest and /embed endpoints).

Coverage:
  1. Input validation (missing/empty/URL-only)
  2. Happy path — full pipeline success, response schema validation
  3. Pipeline error branches (no recipes, refinement failure, embedding failure)
  4. Missing API key ? 500
  5. Unexpected exception ? 500
  6. UOM / measure_preference passthrough to refine_recipe_for_cayenne
  7. Fat Token format preserved in tokenized_directions
  8. AI-converted ingredient fields surfaced correctly
  9. prep_time / cook_time sourced from raw recipe
  10. base_servings fallback (None ? 4)
"""
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

# Must be set BEFORE importing api.py so that HTTPBearer is created with
# auto_error=False (DISABLE_AUTH=1 path).  Without this, FastAPI's bearer
# scheme rejects requests with no Authorization header at the middleware
# layer — before dependency_overrides can intercept them — producing 403/401
# instead of the expected status codes.
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")
os.environ["DISABLE_AUTH"] = "1"

from recipeparser.api import app, _verify_supabase_jwt
from recipeparser.models import (
    RecipeExtraction,
    RecipeList,
    StructuredIngredient,
    TokenizedDirection,
    CayenneRefinement,
)


def _mock_auth() -> dict:
    """Bypass JWT verification in tests. Returns a minimal Supabase-like payload."""
    return {"sub": "test-user-00000000-0000-0000-0000-000000000000"}


app.dependency_overrides[_verify_supabase_jwt] = _mock_auth

client = TestClient(app)

# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

def _make_raw_recipe(**kwargs):
    defaults = dict(
        name="Test Cake",
        ingredients=["1 cup flour", "2 eggs"],
        directions=["Mix well.", "Bake 30 mins."],
        prep_time="10 mins",
        cook_time="30 mins",
        servings="4",
    )
    defaults.update(kwargs)
    return RecipeExtraction(**defaults)


def _make_refined(**kwargs):
    defaults = dict(
        title="Test Cake",
        base_servings=4,
        structured_ingredients=[
            StructuredIngredient(
                id="ing_01",
                amount=1.0,
                unit="cup",
                name="flour",
                fallback_string="1 cup flour",
                converted_amount=None,
                converted_unit=None,
                is_ai_converted=False,
            ),
            StructuredIngredient(
                id="ing_02",
                amount=2.0,
                unit=None,
                name="eggs",
                fallback_string="2 eggs",
                converted_amount=None,
                converted_unit=None,
                is_ai_converted=False,
            ),
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Mix {{ing_01|1 cup flour}} well."),
            TokenizedDirection(step=2, text="Bake 30 mins."),
        ],
    )
    defaults.update(kwargs)
    return CayenneRefinement(**defaults)


FAKE_EMBEDDING = [0.1] * 1536

MOCK_TARGETS = {
    "extract": "recipeparser.gemini.extract_recipe_from_text",
    "refine": "recipeparser.gemini.refine_recipe_for_cayenne",
    "embed": "recipeparser.gemini.get_embeddings",
    "client": "recipeparser.api._get_client",
}


# ---------------------------------------------------------------------------
# 1. Input validation
# ---------------------------------------------------------------------------

def test_ingest_missing_both_url_and_text():
    resp = client.post("/ingest", json={})
    assert resp.status_code == 400
    assert "Only text ingestion" in resp.json()["detail"]


def test_ingest_url_only_not_yet_supported():
    resp = client.post("/ingest", json={"url": "https://example.com/recipe"})
    assert resp.status_code == 400


def test_ingest_empty_text_rejected():
    resp = client.post("/ingest", json={"text": ""})
    assert resp.status_code == 400


def test_ingest_whitespace_text_rejected():
    resp = client.post("/ingest", json={"text": "   "})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 2. Happy path — full pipeline success
# ---------------------------------------------------------------------------

def test_ingest_happy_path_returns_200():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 200


def test_ingest_happy_path_response_schema():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    # Top-level required keys
    for key in ("title", "prep_time", "cook_time", "base_servings",
                "source_url", "categories", "structured_ingredients",
                "tokenized_directions", "embedding"):
        assert key in data, f"Missing key: {key}"


def test_ingest_happy_path_embedding_length():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert len(resp.json()["embedding"]) == 1536


def test_ingest_happy_path_ingredient_schema():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    ing = resp.json()["structured_ingredients"][0]
    for key in ("id", "amount", "unit", "name", "fallback_string",
                "converted_amount", "converted_unit", "is_ai_converted"):
        assert key in ing, f"Ingredient missing key: {key}"


def test_ingest_happy_path_direction_schema():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    direction = resp.json()["tokenized_directions"][0]
    assert "step" in direction
    assert "text" in direction


# ---------------------------------------------------------------------------
# 3. Pipeline error branches
# ---------------------------------------------------------------------------

def test_ingest_no_recipes_found_returns_422():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[])), \
         patch(MOCK_TARGETS["refine"]), \
         patch(MOCK_TARGETS["embed"]):
        resp = client.post("/ingest", json={"text": "Not a recipe"})
    assert resp.status_code == 422
    assert "No recipes found" in resp.json()["detail"]


def test_ingest_extract_returns_none_returns_422():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=None), \
         patch(MOCK_TARGETS["refine"]), \
         patch(MOCK_TARGETS["embed"]):
        resp = client.post("/ingest", json={"text": "Not a recipe"})
    assert resp.status_code == 422


def test_ingest_refinement_returns_none_returns_500():
    raw = _make_raw_recipe()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=None), \
         patch(MOCK_TARGETS["embed"]):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "Refinement pass failed" in resp.json()["detail"]


def test_ingest_embedding_raises_returns_500():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], side_effect=RuntimeError("embed failed")):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# 4. Missing API key ? 500
# ---------------------------------------------------------------------------

def test_ingest_missing_api_key_returns_500():
    with patch("recipeparser.api._get_client", side_effect=RuntimeError("GOOGLE_API_KEY not found")):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "GOOGLE_API_KEY" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Unexpected exception ? 500
# ---------------------------------------------------------------------------

def test_ingest_unexpected_exception_returns_500():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], side_effect=Exception("boom")):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "boom" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 6. UOM / measure_preference passthrough
# ---------------------------------------------------------------------------

def test_ingest_uom_passthrough_to_refine():
    raw = _make_raw_recipe()
    refined = _make_refined()
    mock_refine = MagicMock(return_value=refined)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], mock_refine), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        client.post("/ingest", json={
            "text": "Some recipe text",
            "uom_system": "Metric",
            "measure_preference": "Weight",
        })
    mock_refine.assert_called_once()
    _, kwargs = mock_refine.call_args
    assert kwargs.get("uom_system") == "Metric"
    assert kwargs.get("measure_preference") == "Weight"


def test_ingest_default_uom_is_us_volume():
    raw = _make_raw_recipe()
    refined = _make_refined()
    mock_refine = MagicMock(return_value=refined)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], mock_refine), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        client.post("/ingest", json={"text": "Some recipe text"})
    _, kwargs = mock_refine.call_args
    assert kwargs.get("uom_system") == "US"
    assert kwargs.get("measure_preference") == "Volume"


# ---------------------------------------------------------------------------
# 7. Fat Token format preserved in tokenized_directions
# ---------------------------------------------------------------------------

def test_ingest_fat_token_format_preserved():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    directions = resp.json()["tokenized_directions"]
    step1_text = directions[0]["text"]
    assert "{{ing_01|1 cup flour}}" in step1_text


def test_ingest_direction_steps_are_1_based():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    steps = [d["step"] for d in resp.json()["tokenized_directions"]]
    assert steps[0] == 1


# ---------------------------------------------------------------------------
# 8. AI-converted ingredient fields
# ---------------------------------------------------------------------------

def test_ingest_ai_converted_ingredient_fields():
    raw = _make_raw_recipe()
    refined = _make_refined(
        structured_ingredients=[
            StructuredIngredient(
                id="ing_01",
                amount=1.0,
                unit="cup",
                name="flour",
                fallback_string="1 cup flour",
                converted_amount=120.0,
                converted_unit="g",
                is_ai_converted=True,
            ),
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Mix {{ing_01|1 cup flour}}."),
        ],
    )
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    ing = resp.json()["structured_ingredients"][0]
    assert ing["is_ai_converted"] is True
    assert ing["converted_amount"] == 120.0
    assert ing["converted_unit"] == "g"


# ---------------------------------------------------------------------------
# 9. prep_time / cook_time sourced from raw recipe
# ---------------------------------------------------------------------------

def test_ingest_times_come_from_raw_recipe():
    raw = _make_raw_recipe(prep_time="15 mins", cook_time="45 mins")
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    assert data["prep_time"] == "15 mins"
    assert data["cook_time"] == "45 mins"


def test_ingest_times_none_when_raw_has_none():
    raw = _make_raw_recipe(prep_time=None, cook_time=None)
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    assert data["prep_time"] is None
    assert data["cook_time"] is None


# ---------------------------------------------------------------------------
# 10. base_servings fallback (None ? 4)
# ---------------------------------------------------------------------------

def test_ingest_base_servings_fallback_to_4():
    raw = _make_raw_recipe()
    refined = _make_refined(base_servings=None)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.json()["base_servings"] == 4


def test_ingest_base_servings_uses_refined_value():
    raw = _make_raw_recipe()
    refined = _make_refined(base_servings=6)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.json()["base_servings"] == 6


# ---------------------------------------------------------------------------
# 11. source_url echoed back from request
# ---------------------------------------------------------------------------

def test_ingest_source_url_echoed_when_text_provided():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={
            "text": "Some recipe text",
            "url": "https://example.com/my-recipe",
        })
    assert resp.json()["source_url"] == "https://example.com/my-recipe"


def test_ingest_source_url_is_none_when_not_provided():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.json()["source_url"] is None

# ---------------------------------------------------------------------------
# 12. Embed endpoint
# ---------------------------------------------------------------------------

def test_embed_happy_path():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/embed", json={"text": "search query"})
    assert resp.status_code == 200
    assert resp.json()["embedding"] == FAKE_EMBEDDING


def test_embed_missing_text_returns_422():
    resp = client.post("/embed", json={})
    assert resp.status_code == 422


def test_embed_error_returns_500():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["embed"], side_effect=Exception("API failure")):
        resp = client.post("/embed", json={"text": "query"})
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# 13. /ingest/pdf — text-based PDF (normal path)
# ---------------------------------------------------------------------------

def _make_pdf_bytes(text_per_page: list[str]) -> bytes:
    """
    Build a minimal real PDF in memory using PyMuPDF so the endpoint can
    actually open it.  Each string in text_per_page becomes one page.

    NOTE: PyMuPDF's insert_text() renders text at a fixed font size; short
    strings can fall below the 50-char/page threshold used to detect scanned
    PDFs.  Callers should pass strings of at least 60 characters per page to
    reliably exercise the text-based path.
    """
    import fitz
    doc = fitz.open()
    for text in text_per_page:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    return doc.tobytes()


def _upload_pdf(pdf_bytes: bytes, filename: str = "recipe.pdf", **form_fields):
    """POST multipart/form-data to /ingest/pdf using the TestClient."""
    return client.post(
        "/ingest/pdf",
        files={"file": (filename, pdf_bytes, "application/pdf")},
        data=form_fields,
    )


class TestIngestPdfTextBased:
    """Tests for the normal (text-extractable) PDF path."""

    # Long enough text (> 50 chars/page) to pass the text-based PDF detection threshold.
    _TEXT_PDF_CONTENT = (
        "Chocolate Cake\n"
        "Ingredients: 1 cup flour, 2 eggs, 1 cup sugar, 1/2 cup butter\n"
        "Directions: Mix dry ingredients. Add wet ingredients. Bake at 350F for 30 minutes."
    )

    def test_text_pdf_returns_200(self):
        raw = _make_raw_recipe()
        refined = _make_refined()
        pdf_bytes = _make_pdf_bytes([self._TEXT_PDF_CONTENT])
        with patch(MOCK_TARGETS["client"]), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(MOCK_TARGETS["refine"], return_value=refined), \
             patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
            resp = _upload_pdf(pdf_bytes)
        assert resp.status_code == 200

    def test_text_pdf_response_schema(self):
        raw = _make_raw_recipe()
        refined = _make_refined()
        pdf_bytes = _make_pdf_bytes([self._TEXT_PDF_CONTENT])
        with patch(MOCK_TARGETS["client"]), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(MOCK_TARGETS["refine"], return_value=refined), \
             patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
            resp = _upload_pdf(pdf_bytes)
        data = resp.json()
        for key in ("title", "structured_ingredients", "tokenized_directions", "embedding"):
            assert key in data, f"Missing key: {key}"

    def test_text_pdf_embedding_length(self):
        raw = _make_raw_recipe()
        refined = _make_refined()
        pdf_bytes = _make_pdf_bytes([self._TEXT_PDF_CONTENT])
        with patch(MOCK_TARGETS["client"]), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(MOCK_TARGETS["refine"], return_value=refined), \
             patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
            resp = _upload_pdf(pdf_bytes)
        assert len(resp.json()["embedding"]) == 1536

    def test_text_pdf_uom_passthrough(self):
        raw = _make_raw_recipe()
        refined = _make_refined()
        pdf_bytes = _make_pdf_bytes([self._TEXT_PDF_CONTENT])
        mock_refine = MagicMock(return_value=refined)
        with patch(MOCK_TARGETS["client"]), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(MOCK_TARGETS["refine"], mock_refine), \
             patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
            _upload_pdf(pdf_bytes, uom_system="Metric", measure_preference="Weight")
        _, kwargs = mock_refine.call_args
        assert kwargs.get("uom_system") == "Metric"
        assert kwargs.get("measure_preference") == "Weight"

    def test_non_pdf_extension_returns_400(self):
        resp = client.post(
            "/ingest/pdf",
            files={"file": ("recipe.txt", b"some text", "text/plain")},
        )
        assert resp.status_code == 400
        assert ".pdf" in resp.json()["detail"].lower() or "pdf" in resp.json()["detail"].lower()

    def test_corrupt_pdf_returns_422(self):
        resp = _upload_pdf(b"this is not a pdf at all")
        assert resp.status_code == 422
        assert "Failed to open PDF" in resp.json()["detail"]

    def test_no_recipes_found_returns_422(self):
        pdf_bytes = _make_pdf_bytes([self._TEXT_PDF_CONTENT])
        with patch(MOCK_TARGETS["client"]), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[])), \
             patch(MOCK_TARGETS["refine"]), \
             patch(MOCK_TARGETS["embed"]):
            resp = _upload_pdf(pdf_bytes)
        assert resp.status_code == 422
        assert "No recipes found" in resp.json()["detail"]

    def test_source_url_is_none_for_pdf(self):
        """PDF uploads never have a source_url — it should always be null."""
        raw = _make_raw_recipe()
        refined = _make_refined()
        pdf_bytes = _make_pdf_bytes([self._TEXT_PDF_CONTENT])
        with patch(MOCK_TARGETS["client"]), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(MOCK_TARGETS["refine"], return_value=refined), \
             patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
            resp = _upload_pdf(pdf_bytes)
        assert resp.json()["source_url"] is None


# ---------------------------------------------------------------------------
# 14. /ingest/pdf — scanned PDF (Gemini Vision OCR fallback)
# ---------------------------------------------------------------------------

def _make_image_only_pdf() -> bytes:
    """
    Build a PDF whose pages contain no text layer (simulates a scanned document).
    We insert a tiny white rectangle as the page content — no text at all.
    """
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    # Draw a white rectangle — no text inserted
    page.draw_rect(fitz.Rect(0, 0, 595, 842), color=(1, 1, 1), fill=(1, 1, 1))
    return doc.tobytes()


class TestIngestPdfScannedVisionFallback:
    """Tests for the scanned PDF ? Gemini Vision OCR fallback path."""

    def test_scanned_pdf_triggers_vision_fallback(self):
        """
        A PDF with no text layer should call extract_text_via_vision, not raise 422.
        """
        raw = _make_raw_recipe()
        refined = _make_refined()
        pdf_bytes = _make_image_only_pdf()
        mock_vision = MagicMock(return_value="Chocolate Cake\n1 cup flour\nMix and bake.")
        with patch(MOCK_TARGETS["client"]), \
             patch("recipeparser.gemini.extract_text_via_vision", mock_vision), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(MOCK_TARGETS["refine"], return_value=refined), \
             patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
            resp = _upload_pdf(pdf_bytes)
        assert resp.status_code == 200
        mock_vision.assert_called_once()

    def test_scanned_pdf_returns_full_ingest_response(self):
        """Vision fallback produces the same IngestResponse shape as text-based PDFs."""
        raw = _make_raw_recipe()
        refined = _make_refined()
        pdf_bytes = _make_image_only_pdf()
        with patch(MOCK_TARGETS["client"]), \
             patch("recipeparser.gemini.extract_text_via_vision",
                   return_value="Chocolate Cake\n1 cup flour\nMix and bake."), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(MOCK_TARGETS["refine"], return_value=refined), \
             patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
            resp = _upload_pdf(pdf_bytes)
        data = resp.json()
        for key in ("title", "structured_ingredients", "tokenized_directions", "embedding"):
            assert key in data, f"Missing key: {key}"
        assert len(data["embedding"]) == 1536

    def test_scanned_pdf_vision_receives_open_doc(self):
        """extract_text_via_vision must be called with the fitz.Document (not None)."""
        import fitz
        raw = _make_raw_recipe()
        refined = _make_refined()
        pdf_bytes = _make_image_only_pdf()
        captured_args = {}

        def capture_vision(doc, client_arg):
            captured_args["doc"] = doc
            return "Chocolate Cake\n1 cup flour\nMix and bake."

        with patch(MOCK_TARGETS["client"]), \
             patch("recipeparser.gemini.extract_text_via_vision", side_effect=capture_vision), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(MOCK_TARGETS["refine"], return_value=refined), \
             patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
            _upload_pdf(pdf_bytes)

        assert "doc" in captured_args
        # The doc should be a fitz.Document (or a mock of one in other tests)
        assert captured_args["doc"] is not None

    def test_scanned_pdf_vision_runtime_error_returns_422(self):
        """If Gemini Vision returns no text for any page, the endpoint returns 422."""
        pdf_bytes = _make_image_only_pdf()
        with patch(MOCK_TARGETS["client"]), \
             patch("recipeparser.gemini.extract_text_via_vision",
                   side_effect=RuntimeError("Gemini Vision returned no text for any page")):
            resp = _upload_pdf(pdf_bytes)
        assert resp.status_code == 422
        assert "no text" in resp.json()["detail"].lower()

    def test_scanned_pdf_vision_no_recipes_returns_422(self):
        """Vision OCR succeeds but Gemini finds no recipe in the transcript ? 422."""
        pdf_bytes = _make_image_only_pdf()
        with patch(MOCK_TARGETS["client"]), \
             patch("recipeparser.gemini.extract_text_via_vision",
                   return_value="This is a scanned page with no recipe content."), \
             patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[])), \
             patch(MOCK_TARGETS["refine"]), \
             patch(MOCK_TARGETS["embed"]):
            resp = _upload_pdf(pdf_bytes)
        assert resp.status_code == 422
        assert "No recipes found" in resp.json()["detail"]

    def test_scanned_pdf_vision_missing_api_key_returns_500(self):
        """If GOOGLE_API_KEY is missing when vision path is triggered, return 500."""
        pdf_bytes = _make_image_only_pdf()
        with patch("recipeparser.api._get_client",
                   side_effect=RuntimeError("GOOGLE_API_KEY not found")):
            resp = _upload_pdf(pdf_bytes)
        assert resp.status_code == 500
        assert "GOOGLE_API_KEY" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 15. _extract_image_url_from_markdown — unit tests (no HTTP, no Gemini)
# ---------------------------------------------------------------------------

from recipeparser.api import _extract_image_url_from_markdown


class TestExtractImageUrlFromMarkdown:
    """Pure-unit tests for the og:image / Markdown image URL extractor."""

    # --- og:image / twitter:image priority ---

    def test_og_image_colon_syntax(self):
        md = 'og:image: https://example.com/photo.jpg\nSome other content'
        assert _extract_image_url_from_markdown(md) == 'https://example.com/photo.jpg'

    def test_og_image_with_surrounding_whitespace(self):
        """og:image with extra spaces around the colon and URL."""
        md = 'og:image:   https://cdn.example.com/hero.png  \nMore content'
        result = _extract_image_url_from_markdown(md)
        assert result is not None
        assert 'hero.png' in result

    def test_twitter_image_colon_syntax(self):
        md = 'twitter:image: https://example.com/twitter-card.jpg'
        assert _extract_image_url_from_markdown(md) == 'https://example.com/twitter-card.jpg'

    def test_og_image_takes_priority_over_markdown_image(self):
        md = (
            '![alt text](https://example.com/inline.jpg)\n'
            'og:image: https://example.com/og-hero.jpg\n'
        )
        result = _extract_image_url_from_markdown(md)
        assert result == 'https://example.com/og-hero.jpg'

    # --- Markdown image fallback ---

    def test_markdown_image_returned_when_no_og(self):
        md = 'Some intro text\n![Recipe photo](https://example.com/cake.jpg)\nMore text'
        assert _extract_image_url_from_markdown(md) == 'https://example.com/cake.jpg'

    def test_first_markdown_image_returned_when_multiple(self):
        md = (
            '![First](https://example.com/first.jpg)\n'
            '![Second](https://example.com/second.jpg)\n'
        )
        assert _extract_image_url_from_markdown(md) == 'https://example.com/first.jpg'

    def test_markdown_image_with_empty_alt(self):
        md = '![](https://example.com/no-alt.webp)'
        assert _extract_image_url_from_markdown(md) == 'https://example.com/no-alt.webp'

    # --- No image found ---

    def test_returns_none_when_no_image(self):
        md = 'Just plain text with no images at all.'
        assert _extract_image_url_from_markdown(md) is None

    def test_returns_none_for_empty_string(self):
        assert _extract_image_url_from_markdown('') is None

    def test_returns_none_for_relative_image_path(self):
        """Relative paths (no http/https) must not be returned — they are unusable."""
        md = '![alt](/images/local.jpg)'
        assert _extract_image_url_from_markdown(md) is None

    def test_returns_none_for_data_uri(self):
        """data: URIs are not valid remote image URLs."""
        md = '![alt](data:image/png;base64,abc123)'
        assert _extract_image_url_from_markdown(md) is None

    # --- URL cleaning ---

    def test_trailing_paren_stripped_from_og_image_when_double(self):
        """When Jina captures an extra ')' (e.g. meta in parens), remove only one trailing ')'.

        We do NOT use rstrip(')') — that would corrupt URLs containing parentheses,
        e.g. https://example.com/path(name) would become .../path(name .
        """
        md = 'og:image: https://example.com/photo.jpg))'
        result = _extract_image_url_from_markdown(md)
        assert result == 'https://example.com/photo.jpg)'

    def test_url_with_parens_preserved(self):
        """Legitimate URL ending with ')' must not be stripped (e.g. Wikipedia, link in parens)."""
        md = 'og:image: https://example.com/wiki/Recipe_(disambiguation)'
        result = _extract_image_url_from_markdown(md)
        assert result == 'https://example.com/wiki/Recipe_(disambiguation)'

    def test_url_with_parens_plus_extraneous_paren(self):
        """URL has ')' in path and Jina adds one extra ')' — remove only the extraneous one."""
        md = 'og:image: https://example.com/path(name))'
        result = _extract_image_url_from_markdown(md)
        assert result == 'https://example.com/path(name)'

    def test_https_url_preserved(self):
        md = '![](https://cdn.example.com/path/to/image.png)'
        result = _extract_image_url_from_markdown(md)
        assert result == 'https://cdn.example.com/path/to/image.png'

    def test_http_url_accepted(self):
        """http:// URLs are valid (some CDNs still use plain HTTP)."""
        md = '![](http://example.com/image.jpg)'
        result = _extract_image_url_from_markdown(md)
        assert result == 'http://example.com/image.jpg'

    # --- Real-world Jina markdown patterns ---

    def test_jina_style_og_image_block(self):
        """Simulate the kind of meta block Jina surfaces in its markdown output."""
        md = (
            'Title: Chocolate Lava Cake\n'
            'og:image: https://www.seriouseats.com/thmb/abc123/hero.jpg\n'
            'description: A rich, gooey chocolate dessert.\n'
            '\n'
            '## Ingredients\n'
            '- 4 oz dark chocolate\n'
        )
        result = _extract_image_url_from_markdown(md)
        assert result == 'https://www.seriouseats.com/thmb/abc123/hero.jpg'

    def test_jina_style_inline_image_in_recipe_body(self):
        """Simulate a Jina page where the hero image appears inline in the article."""
        md = (
            '# Chocolate Lava Cake\n'
            '\n'
            '![Chocolate Lava Cake](https://www.seriouseats.com/thmb/abc123/hero.jpg)\n'
            '\n'
            '## Ingredients\n'
            '- 4 oz dark chocolate\n'
        )
        result = _extract_image_url_from_markdown(md)
        assert result == 'https://www.seriouseats.com/thmb/abc123/hero.jpg'


# ---------------------------------------------------------------------------
# 16-27. /ingest/url endpoint  image extraction + upload pipeline
# (Appended from test_ingest_url.py)
# ---------------------------------------------------------------------------
RECIPE_MARKDOWN_WITH_OG = (
    "Title: Chocolate Cake\n"
    "og:image: https://example.com/cake.jpg\n"
    "\n"
    "# Chocolate Cake\n"
    "## Ingredients\n"
    "- 1 cup flour\n"
    "- 2 eggs\n"
    "## Directions\n"
    "Mix and bake at 350F for 30 minutes.\n"
)

RECIPE_MARKDOWN_WITH_MD_IMAGE = (
    "# Chocolate Cake\n"
    "\n"
    "![Chocolate Cake](https://example.com/cake-inline.jpg)\n"
    "\n"
    "## Ingredients\n"
    "- 1 cup flour\n"
    "- 2 eggs\n"
    "## Directions\n"
    "Mix and bake at 350F for 30 minutes.\n"
)

RECIPE_MARKDOWN_NO_IMAGE = (
    "# Chocolate Cake\n"
    "## Ingredients\n"
    "- 1 cup flour\n"
    "- 2 eggs\n"
    "## Directions\n"
    "Mix and bake at 350F for 30 minutes.\n"
)

NOT_A_RECIPE_MARKDOWN = (
    "# Welcome to Our Blog\n"
    "This is a general interest article about cooking history.\n"
    "No ingredients or directions here.\n"
)


def _make_raw_recipe(**kwargs):
    defaults = dict(
        name="Chocolate Cake",
        ingredients=["1 cup flour", "2 eggs"],
        directions=["Mix well.", "Bake 30 mins."],
        prep_time="10 mins",
        cook_time="30 mins",
        servings="4",
    )
    defaults.update(kwargs)
    return RecipeExtraction(**defaults)


def _make_refined(**kwargs):
    defaults = dict(
        title="Chocolate Cake",
        base_servings=4,
        structured_ingredients=[
            StructuredIngredient(
                id="ing_01",
                amount=1.0,
                unit="cup",
                name="flour",
                fallback_string="1 cup flour",
                converted_amount=None,
                converted_unit=None,
                is_ai_converted=False,
            ),
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Mix {{ing_01|1 cup flour}} well."),
        ],
    )
    defaults.update(kwargs)
    return CayenneRefinement(**defaults)


def _make_jina_response(markdown: str, status_code: int = 200):
    """Build a mock httpx Response for the Jina fetch."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = markdown
    mock_resp.raise_for_status = MagicMock()
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return mock_resp


def _make_image_download_response(content: bytes = b"fake-image-bytes",
                                  content_type: str = "image/jpeg",
                                  status_code: int = 200):
    """Build a mock httpx Response for the image download."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = content
    mock_resp.headers = {"content-type": content_type}
    mock_resp.raise_for_status = MagicMock()
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return mock_resp


def _make_supabase_upload_response(status_code: int = 200):
    """Build a mock httpx Response for the Supabase Storage upload."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.raise_for_status = MagicMock()
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return mock_resp


URL_MOCK_TARGETS = {
    "extract": "recipeparser.gemini.extract_recipe_from_text",
    "refine": "recipeparser.gemini.refine_recipe_for_cayenne",
    "embed": "recipeparser.gemini.get_embeddings",
    "gemini_client": "recipeparser.api._get_client",
    "is_recipe": "recipeparser.epub.is_recipe_candidate",
    "html_to_text": "recipeparser.api.html_to_text",
    "httpx_async": "httpx.AsyncClient",
    "httpx_sync": "httpx.Client",
}

# placeholder — tests filled in below


# ---------------------------------------------------------------------------
# Helper: build a fully-mocked /ingest/url call
# ---------------------------------------------------------------------------

def _post_ingest_url(url: str = "https://example.com/recipe", **body_kwargs):
    """POST to /ingest/url with the given URL."""
    return client.post("/ingest/url", json={"url": url, **body_kwargs})


def _pipeline_patches(raw, refined):
    """Return a list of (target, kwargs) for the standard pipeline mocks."""
    return [
        (URL_MOCK_TARGETS["extract"], dict(return_value=RecipeList(recipes=[raw]))),
        (URL_MOCK_TARGETS["refine"],  dict(return_value=refined)),
        (URL_MOCK_TARGETS["embed"],   dict(return_value=FAKE_EMBEDDING)),
        (URL_MOCK_TARGETS["gemini_client"], {}),
    ]


# ---------------------------------------------------------------------------
# 16. Happy path — og:image found ? downloaded ? uploaded ? image_url set
# ---------------------------------------------------------------------------

class TestIngestUrlOgImage:
    """og:image in Jina markdown ? image uploaded ? image_url in response."""

    def _run(self, extra_env=None):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_WITH_OG)
        img_dl_resp = _make_image_download_response()
        sb_up_resp = _make_supabase_upload_response()

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        sync_ctx_dl = MagicMock()
        sync_ctx_dl.__enter__ = MagicMock(return_value=MagicMock(
            get=MagicMock(return_value=img_dl_resp)
        ))
        sync_ctx_dl.__exit__ = MagicMock(return_value=False)

        sync_ctx_up = MagicMock()
        sync_ctx_up.__enter__ = MagicMock(return_value=MagicMock(
            post=MagicMock(return_value=sb_up_resp)
        ))
        sync_ctx_up.__exit__ = MagicMock(return_value=False)

        env_patch = {
            "SUPABASE_URL": "https://proj.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key-abc",
        }
        if extra_env:
            env_patch.update(extra_env)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch("httpx.Client", side_effect=[sync_ctx_dl, sync_ctx_up]), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]), \
             patch.dict("os.environ", env_patch):
            return _post_ingest_url()

    def test_returns_200(self):
        resp = self._run()
        assert resp.status_code == 200

    def test_image_url_is_set(self):
        resp = self._run()
        data = resp.json()
        assert "image_url" in data
        assert data["image_url"] is not None
        assert "supabase.co" in data["image_url"]

    def test_image_url_contains_recipe_images_bucket(self):
        resp = self._run()
        assert "recipe-images" in resp.json()["image_url"]

    def test_response_schema_complete(self):
        resp = self._run()
        data = resp.json()
        for key in ("title", "prep_time", "cook_time", "base_servings",
                    "source_url", "categories", "structured_ingredients",
                    "tokenized_directions", "embedding", "image_url"):
            assert key in data, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# 17. Happy path — Markdown image found ? downloaded ? uploaded ? image_url set
# ---------------------------------------------------------------------------

class TestIngestUrlMarkdownImage:
    """Markdown image tag in Jina output ? image uploaded ? image_url in response."""

    def _run(self):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_WITH_MD_IMAGE)
        img_dl_resp = _make_image_download_response()
        sb_up_resp = _make_supabase_upload_response()

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        sync_ctx_dl = MagicMock()
        sync_ctx_dl.__enter__ = MagicMock(return_value=MagicMock(
            get=MagicMock(return_value=img_dl_resp)
        ))
        sync_ctx_dl.__exit__ = MagicMock(return_value=False)

        sync_ctx_up = MagicMock()
        sync_ctx_up.__enter__ = MagicMock(return_value=MagicMock(
            post=MagicMock(return_value=sb_up_resp)
        ))
        sync_ctx_up.__exit__ = MagicMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch("httpx.Client", side_effect=[sync_ctx_dl, sync_ctx_up]), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]), \
             patch.dict("os.environ", {
                 "SUPABASE_URL": "https://proj.supabase.co",
                 "SUPABASE_SERVICE_KEY": "service-key-abc",
             }):
            return _post_ingest_url()

    def test_returns_200(self):
        assert self._run().status_code == 200

    def test_image_url_is_set(self):
        data = self._run().json()
        assert data.get("image_url") is not None
        assert "supabase.co" in data["image_url"]


# ---------------------------------------------------------------------------
# 18. No image in markdown ? image_url is None, pipeline still succeeds
# ---------------------------------------------------------------------------

class TestIngestUrlNoImage:
    """No image in Jina markdown ? image_url is None, recipe still returned."""

    def _run(self):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_NO_IMAGE)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            return _post_ingest_url()

    def test_returns_200(self):
        assert self._run().status_code == 200

    def test_image_url_is_none(self):
        assert self._run().json().get("image_url") is None


# ---------------------------------------------------------------------------
# 19. Image download fails ? image_url is None, pipeline still succeeds
# ---------------------------------------------------------------------------

class TestIngestUrlImageDownloadFails:
    """httpx raises during image download ? non-fatal, image_url is None."""

    def _run(self):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_WITH_OG)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        # Sync httpx.Client raises on image download
        sync_ctx_dl = MagicMock()
        sync_ctx_dl.__enter__ = MagicMock(return_value=MagicMock(
            get=MagicMock(side_effect=Exception("Connection refused"))
        ))
        sync_ctx_dl.__exit__ = MagicMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch("httpx.Client", return_value=sync_ctx_dl), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]), \
             patch.dict("os.environ", {
                 "SUPABASE_URL": "https://proj.supabase.co",
                 "SUPABASE_SERVICE_KEY": "service-key-abc",
             }):
            return _post_ingest_url()

    def test_returns_200(self):
        assert self._run().status_code == 200

    def test_image_url_is_none(self):
        assert self._run().json().get("image_url") is None


# ---------------------------------------------------------------------------
# 20. Supabase upload fails ? image_url is None, pipeline still succeeds
# ---------------------------------------------------------------------------

class TestIngestUrlSupabaseUploadFails:
    """Supabase Storage POST raises ? non-fatal, image_url is None."""

    def _run(self):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_WITH_OG)
        img_dl_resp = _make_image_download_response()

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        sync_ctx_dl = MagicMock()
        sync_ctx_dl.__enter__ = MagicMock(return_value=MagicMock(
            get=MagicMock(return_value=img_dl_resp)
        ))
        sync_ctx_dl.__exit__ = MagicMock(return_value=False)

        # Second httpx.Client (upload) raises
        sync_ctx_up = MagicMock()
        sync_ctx_up.__enter__ = MagicMock(return_value=MagicMock(
            post=MagicMock(side_effect=Exception("Supabase 503"))
        ))
        sync_ctx_up.__exit__ = MagicMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch("httpx.Client", side_effect=[sync_ctx_dl, sync_ctx_up]), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]), \
             patch.dict("os.environ", {
                 "SUPABASE_URL": "https://proj.supabase.co",
                 "SUPABASE_SERVICE_KEY": "service-key-abc",
             }):
            return _post_ingest_url()

    def test_returns_200(self):
        assert self._run().status_code == 200

    def test_image_url_is_none(self):
        assert self._run().json().get("image_url") is None


# ---------------------------------------------------------------------------
# 21. Missing SUPABASE_URL / SUPABASE_SERVICE_KEY ? upload skipped, image_url None
# ---------------------------------------------------------------------------

class TestIngestUrlMissingSupabaseEnv:
    """When Supabase env vars are absent, upload is skipped silently."""

    def _run_with_env(self, env_overrides: dict):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_WITH_OG)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]), \
             patch.dict("os.environ", env_overrides, clear=False):
            # Remove the keys if they exist
            for key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
                os.environ.pop(key, None)
            return _post_ingest_url()

    def test_missing_supabase_url_returns_200(self):
        resp = self._run_with_env({})
        assert resp.status_code == 200

    def test_missing_supabase_url_image_url_is_none(self):
        resp = self._run_with_env({})
        assert resp.json().get("image_url") is None

    def test_missing_service_key_returns_200(self):
        resp = self._run_with_env({"SUPABASE_URL": "https://proj.supabase.co"})
        assert resp.status_code == 200

    def test_missing_service_key_image_url_is_none(self):
        resp = self._run_with_env({"SUPABASE_URL": "https://proj.supabase.co"})
        assert resp.json().get("image_url") is None


# ---------------------------------------------------------------------------
# 22. Jina fetch fails ? 422
# ---------------------------------------------------------------------------

class TestIngestUrlJinaFails:
    """If the Jina HTTP fetch raises, the endpoint returns 422."""

    def test_jina_connection_error_returns_422(self):
        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(side_effect=Exception("Connection refused"))
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            resp = _post_ingest_url()

        assert resp.status_code == 422
        assert "Failed to fetch URL" in resp.json()["detail"]

    def test_jina_http_error_returns_422(self):
        jina_resp = _make_jina_response("", status_code=503)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            resp = _post_ingest_url()

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 23. URL not a recipe ? 422
# ---------------------------------------------------------------------------

class TestIngestUrlNotARecipe:
    """If is_recipe_candidate returns False, the endpoint returns 422."""

    def test_non_recipe_url_returns_422(self):
        jina_resp = _make_jina_response(NOT_A_RECIPE_MARKDOWN)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=False), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value=NOT_A_RECIPE_MARKDOWN), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            resp = _post_ingest_url()

        assert resp.status_code == 422
        assert "recipe" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 24. UOM passthrough to pipeline
# ---------------------------------------------------------------------------

class TestIngestUrlUomPassthrough:
    """uom_system and measure_preference are forwarded to refine_recipe_for_cayenne."""

    def test_metric_weight_passthrough(self):
        raw = _make_raw_recipe()
        refined = _make_refined()
        mock_refine = MagicMock(return_value=refined)

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_NO_IMAGE)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], mock_refine), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            _post_ingest_url(uom_system="Metric", measure_preference="Weight")

        mock_refine.assert_called_once()
        _, kwargs = mock_refine.call_args
        assert kwargs.get("uom_system") == "Metric"
        assert kwargs.get("measure_preference") == "Weight"

    def test_default_uom_is_us_volume(self):
        raw = _make_raw_recipe()
        refined = _make_refined()
        mock_refine = MagicMock(return_value=refined)

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_NO_IMAGE)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], mock_refine), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            _post_ingest_url()

        _, kwargs = mock_refine.call_args
        assert kwargs.get("uom_system") == "US"
        assert kwargs.get("measure_preference") == "Volume"


# ---------------------------------------------------------------------------
# 25. source_url echoed in response
# ---------------------------------------------------------------------------

class TestIngestUrlSourceUrl:
    """The submitted URL is echoed back as source_url in the response."""

    def _run(self, url: str):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_NO_IMAGE)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            return _post_ingest_url(url=url)

    def test_source_url_echoed(self):
        url = "https://www.seriouseats.com/chocolate-cake"
        resp = self._run(url)
        assert resp.status_code == 200
        assert resp.json()["source_url"] == url


# ---------------------------------------------------------------------------
# 26. Missing url field ? 400
# ---------------------------------------------------------------------------

class TestIngestUrlMissingField:
    """Sending an empty or missing url field returns 400."""

    def test_empty_url_returns_400(self):
        resp = client.post("/ingest/url", json={"url": ""})
        assert resp.status_code == 400
        assert "url" in resp.json()["detail"].lower()

    def test_missing_url_field_returns_422(self):
        # Pydantic will reject a missing required field with 422
        resp = client.post("/ingest/url", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 27. Full response schema validated (all keys present)
# ---------------------------------------------------------------------------

class TestIngestUrlResponseSchema:
    """The /ingest/url response includes all required IngestResponse fields."""

    def test_all_required_keys_present(self):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_NO_IMAGE)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            resp = _post_ingest_url()

        assert resp.status_code == 200
        data = resp.json()
        required_keys = (
            "title", "prep_time", "cook_time", "base_servings",
            "source_url", "categories", "structured_ingredients",
            "tokenized_directions", "embedding", "image_url",
        )
        for key in required_keys:
            assert key in data, f"Missing key in response: {key}"

    def test_embedding_length_is_1536(self):
        raw = _make_raw_recipe()
        refined = _make_refined()

        jina_resp = _make_jina_response(RECIPE_MARKDOWN_NO_IMAGE)

        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=jina_resp)
        ))
        async_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_ctx), \
             patch(URL_MOCK_TARGETS["is_recipe"], return_value=True), \
             patch(URL_MOCK_TARGETS["html_to_text"], return_value="Chocolate Cake 1 cup flour Mix and bake."), \
             patch(URL_MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
             patch(URL_MOCK_TARGETS["refine"], return_value=refined), \
             patch(URL_MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING), \
             patch(URL_MOCK_TARGETS["gemini_client"]):
            resp = _post_ingest_url()

        assert len(resp.json()["embedding"]) == 1536
