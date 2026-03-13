"""Tests for the Cayenne Ingestion API (/ingest and /embed endpoints).

Coverage:
  1. Input validation (missing/empty/URL-only)
  2. Happy path — full pipeline success, response schema validation
  3. Pipeline error branches (no recipes, refinement failure, embedding failure)
  4. Missing API key → 500
  5. Unexpected exception → 500
  6. UOM / measure_preference passthrough to refine_recipe_for_cayenne
  7. Fat Token format preserved in tokenized_directions
  8. AI-converted ingredient fields surfaced correctly
  9. prep_time / cook_time sourced from raw recipe
  10. base_servings fallback (None → 4)
"""
import os
import pytest
from unittest.mock import patch, MagicMock
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
# 4. Missing API key → 500
# ---------------------------------------------------------------------------

def test_ingest_missing_api_key_returns_500():
    with patch("recipeparser.api._get_client", side_effect=RuntimeError("GOOGLE_API_KEY not found")):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "GOOGLE_API_KEY" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Unexpected exception → 500
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
# 10. base_servings fallback (None → 4)
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
    """Tests for the scanned PDF → Gemini Vision OCR fallback path."""

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
        """Vision OCR succeeds but Gemini finds no recipe in the transcript → 422."""
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
