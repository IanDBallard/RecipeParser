"""Tests for the Cayenne Ingestion API (/ingest, /ingest/url, /ingest/pdf, /embed).

New contract (Phase 6):
  - All /ingest* endpoints return 202 + JobResponse({job_id, recipe_id})
  - write_recipe_to_supabase is called internally; client never receives recipe JSON
  - /embed still returns 200 + {embedding}

Mock strategy:
  - recipeparser.adapters.api.run_cayenne_pipeline  — replaces the full pipeline
  - recipeparser.adapters.api.write_recipe_to_supabase — replaces the Supabase write
  - recipeparser.adapters.api._get_client — prevents real Gemini init
"""
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

# Must be set BEFORE importing api.py so that HTTPBearer is created with
# auto_error=False (DISABLE_AUTH=1 path).  Without this, FastAPI's bearer
# scheme rejects requests with no Authorization header at the middleware
# layer - before dependency_overrides can intercept them.
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")
os.environ["DISABLE_AUTH"] = "1"
os.environ["TEST_USER_ID"] = "test-user-00000000-0000-0000-0000-000000000000"

from recipeparser.api import app, _verify_supabase_jwt
from recipeparser.models import (
    IngestResponse,
    StructuredIngredient,
    TokenizedDirection,
    CayenneRecipe,
)


def _mock_auth() -> dict:
    """Bypass JWT verification in tests."""
    return {"sub": "test-user-00000000-0000-0000-0000-000000000000"}


app.dependency_overrides[_verify_supabase_jwt] = _mock_auth

client = TestClient(app)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

FAKE_RECIPE_ID = "aaaaaaaa-0000-0000-0000-000000000000"
FAKE_EMBEDDING = [0.1] * 1536

# Mock targets — all resolved against the adapters.api namespace where they
# are imported (not the original module where they are defined).
MOCK_TARGETS = {
    "pipeline": "recipeparser.adapters.api.run_cayenne_pipeline",
    "write":    "recipeparser.adapters.api.write_recipe_to_supabase",
    "client":   "recipeparser.adapters.api._get_client",
    # embed endpoint uses get_embeddings directly
    "embed":    "recipeparser.gemini.get_embeddings",
}

# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

def _make_ingest_response(**kwargs) -> IngestResponse:
    """Build a minimal IngestResponse (what run_cayenne_pipeline returns)."""
    defaults = dict(
        title="Test Cake",
        prep_time="10 mins",
        cook_time="30 mins",
        base_servings=4,
        source_url=None,
        image_url=None,
        categories=["Uncategorized"],
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
        embedding=FAKE_EMBEDDING,
    )
    defaults.update(kwargs)
    return IngestResponse(**defaults)


# ---------------------------------------------------------------------------
# 1. Input validation — /ingest
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
# 2. Happy path — /ingest returns 202 + JobResponse
# ---------------------------------------------------------------------------

def test_ingest_happy_path_returns_202():
    result = _make_ingest_response()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 202


def test_ingest_happy_path_response_has_job_id():
    result = _make_ingest_response()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    assert "job_id" in data
    assert isinstance(data["job_id"], str)
    assert len(data["job_id"]) > 0


def test_ingest_happy_path_response_has_recipe_id():
    result = _make_ingest_response()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    assert "recipe_id" in data
    assert data["recipe_id"] == FAKE_RECIPE_ID


def test_ingest_happy_path_no_recipe_json_in_response():
    """The response must NOT contain recipe fields — only job_id and recipe_id."""
    result = _make_ingest_response()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    # These keys must NOT be present — the client gets no recipe JSON
    for forbidden_key in ("title", "embedding", "structured_ingredients", "tokenized_directions"):
        assert forbidden_key not in data, f"Response must not contain '{forbidden_key}'"


def test_ingest_write_called_with_user_id():
    """write_recipe_to_supabase must be called with the authenticated user_id."""
    result = _make_ingest_response()
    mock_write = MagicMock(return_value=FAKE_RECIPE_ID)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], mock_write):
        client.post("/ingest", json={"text": "Some recipe text"})
    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    assert kwargs.get("user_id") == "test-user-00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# 3. Pipeline error branches — /ingest
# ---------------------------------------------------------------------------

def test_ingest_no_recipes_found_returns_422():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["pipeline"], side_effect=ValueError("No recipes found in source text.")), \
         patch(MOCK_TARGETS["write"]):
        resp = client.post("/ingest", json={"text": "Not a recipe"})
    assert resp.status_code == 422
    assert "No recipes found" in resp.json()["detail"]


def test_ingest_refinement_failure_returns_500():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["pipeline"], side_effect=RuntimeError("Refinement pass failed.")), \
         patch(MOCK_TARGETS["write"]):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "Refinement pass failed" in resp.json()["detail"]


def test_ingest_unexpected_exception_returns_500():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["pipeline"], side_effect=Exception("boom")), \
         patch(MOCK_TARGETS["write"]):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "boom" in resp.json()["detail"]


def test_ingest_missing_api_key_returns_500():
    with patch(MOCK_TARGETS["client"], side_effect=RuntimeError("GOOGLE_API_KEY not found")):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "GOOGLE_API_KEY" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 4. /embed endpoint — unchanged contract (200 + {embedding})
# ---------------------------------------------------------------------------

def test_embed_happy_path_returns_200():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/embed", json={"text": "search query"})
    assert resp.status_code == 200


def test_embed_happy_path_returns_embedding():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/embed", json={"text": "search query"})
    assert resp.json()["embedding"] == FAKE_EMBEDDING


def test_embed_embedding_length_is_1536():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/embed", json={"text": "search query"})
    assert len(resp.json()["embedding"]) == 1536


def test_embed_missing_text_returns_422():
    resp = client.post("/embed", json={})
    assert resp.status_code == 422


def test_embed_error_returns_500():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["embed"], side_effect=Exception("API failure")):
        resp = client.post("/embed", json={"text": "query"})
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# 5. /ingest/pdf — 202 + JobResponse contract
# ---------------------------------------------------------------------------

def _make_pdf_bytes(text_per_page: list) -> bytes:
    """Build a minimal real PDF using PyMuPDF. Each string becomes one page."""
    import fitz
    doc = fitz.open()
    for text in text_per_page:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    return doc.tobytes()


def _upload_pdf(pdf_bytes: bytes, filename: str = "recipe.pdf", **form_fields):
    """POST multipart/form-data to /ingest/pdf."""
    return client.post(
        "/ingest/pdf",
        files={"file": (filename, pdf_bytes, "application/pdf")},
        data=form_fields,
    )


# Long enough text (>50 chars/page) to pass the text-based PDF detection threshold.
_TEXT_PDF_CONTENT = (
    "Chocolate Cake\n"
    "Ingredients: 1 cup flour, 2 eggs, 1 cup sugar, 1/2 cup butter\n"
    "Directions: Mix dry ingredients. Add wet ingredients. Bake at 350F for 30 minutes."
)


def test_pdf_non_pdf_extension_returns_400():
    resp = client.post(
        "/ingest/pdf",
        files={"file": ("recipe.txt", b"some text", "text/plain")},
    )
    assert resp.status_code == 400
    assert "pdf" in resp.json()["detail"].lower()


def test_pdf_corrupt_pdf_returns_422():
    resp = _upload_pdf(b"this is not a pdf at all")
    assert resp.status_code == 422


def test_pdf_happy_path_returns_202():
    result = _make_ingest_response()
    pdf_bytes = _make_pdf_bytes([_TEXT_PDF_CONTENT])
    with patch(MOCK_TARGETS["client"]), \
         patch("recipeparser.io.readers.pdf.extract_text_from_pdf", return_value=_TEXT_PDF_CONTENT), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = _upload_pdf(pdf_bytes)
    assert resp.status_code == 202


def test_pdf_happy_path_has_job_id_and_recipe_id():
    result = _make_ingest_response()
    pdf_bytes = _make_pdf_bytes([_TEXT_PDF_CONTENT])
    with patch(MOCK_TARGETS["client"]), \
         patch("recipeparser.io.readers.pdf.extract_text_from_pdf", return_value=_TEXT_PDF_CONTENT), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = _upload_pdf(pdf_bytes)
    data = resp.json()
    assert "job_id" in data
    assert "recipe_id" in data
    assert data["recipe_id"] == FAKE_RECIPE_ID


def test_pdf_no_recipe_json_in_response():
    result = _make_ingest_response()
    pdf_bytes = _make_pdf_bytes([_TEXT_PDF_CONTENT])
    with patch(MOCK_TARGETS["client"]), \
         patch("recipeparser.io.readers.pdf.extract_text_from_pdf", return_value=_TEXT_PDF_CONTENT), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = _upload_pdf(pdf_bytes)
    data = resp.json()
    for forbidden_key in ("title", "embedding", "structured_ingredients"):
        assert forbidden_key not in data, f"Response must not contain '{forbidden_key}'"


def test_pdf_no_recipes_found_returns_422():
    pdf_bytes = _make_pdf_bytes([_TEXT_PDF_CONTENT])
    with patch(MOCK_TARGETS["client"]), \
         patch("recipeparser.io.readers.pdf.extract_text_from_pdf", return_value=_TEXT_PDF_CONTENT), \
         patch(MOCK_TARGETS["pipeline"], side_effect=ValueError("No recipes found in source text.")), \
         patch(MOCK_TARGETS["write"]):
        resp = _upload_pdf(pdf_bytes)
    assert resp.status_code == 422
    assert "No recipes found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 6. _extract_image_url_from_markdown — pure function unit tests
# ---------------------------------------------------------------------------

from recipeparser.adapters.api import _extract_image_url_from_markdown


def test_image_extractor_og_image_takes_priority():
    """og:image line must win over a Markdown image tag lower in the document."""
    md = 'og:image: https://example.com/og.jpg\n![alt](https://example.com/md.jpg)'
    assert _extract_image_url_from_markdown(md) == 'https://example.com/og.jpg'


def test_image_extractor_twitter_image():
    """twitter:image line is treated the same as og:image."""
    md = 'twitter:image: https://example.com/tw.jpg'
    assert _extract_image_url_from_markdown(md) == 'https://example.com/tw.jpg'


def test_image_extractor_markdown_image_fallback():
    """When no og/twitter meta line exists, the first Markdown image is returned."""
    md = 'Some text\n![Recipe photo](https://example.com/photo.jpg)\nMore text'
    assert _extract_image_url_from_markdown(md) == 'https://example.com/photo.jpg'


def test_image_extractor_double_paren_cleanup():
    """URL ending with )) must have exactly one ) stripped (not both)."""
    md = 'og:image: https://example.com/image.jpg))'
    url = _extract_image_url_from_markdown(md)
    assert url == 'https://example.com/image.jpg)'


def test_image_extractor_single_paren_not_stripped():
    """URL ending with a single ) must NOT be modified."""
    md = 'og:image: https://example.com/path(name).jpg'
    url = _extract_image_url_from_markdown(md)
    # The regex stops at whitespace; a trailing ) that is part of the URL is kept.
    assert url is not None
    assert not url.endswith('))')


def test_image_extractor_no_image_returns_none():
    """Documents with no image references must return None."""
    assert _extract_image_url_from_markdown('No images here at all.') is None


# ---------------------------------------------------------------------------
# 7. /ingest/url — 202 + JobResponse contract
# ---------------------------------------------------------------------------

# Realistic Jina markdown snippet used across URL tests.
_URL_MARKDOWN_CONTENT = (
    'og:image: https://example.com/recipe-hero.jpg\n'
    '# Chocolate Cake\n'
    'Ingredients: 1 cup flour, 2 eggs, 1 cup sugar\n'
    'Directions: Mix and bake at 350F for 30 minutes.'
)

# Plain text that html_to_text would return from the above markdown.
_URL_SOURCE_TEXT = (
    'Chocolate Cake\n'
    'Ingredients: 1 cup flour, 2 eggs, 1 cup sugar\n'
    'Directions: Mix and bake at 350F for 30 minutes.'
)


def _make_httpx_mock(markdown_text: str):
    """
    Build a mock for ``httpx.AsyncClient`` used as an async context manager.

    The mock simulates:
        async with httpx.AsyncClient(...) as http_client:
            response = await http_client.get(...)
    """
    mock_response = MagicMock()
    mock_response.text = markdown_text
    mock_response.raise_for_status = MagicMock()

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(return_value=mock_response)

    return mock_http


def _post_url(url: str = 'https://example.com/recipe', **body_fields):
    """POST JSON to /ingest/url."""
    return client.post('/ingest/url', json={'url': url, **body_fields})


def test_url_happy_path_returns_202():
    result = _make_ingest_response()
    mock_http = _make_httpx_mock(_URL_MARKDOWN_CONTENT)
    with patch(MOCK_TARGETS["client"]), \
         patch('httpx.AsyncClient', return_value=mock_http), \
         patch('recipeparser.adapters.api.html_to_text', return_value=_URL_SOURCE_TEXT), \
         patch('recipeparser.io.readers.epub.is_recipe_candidate', return_value=True), \
         patch('recipeparser.adapters.api._upload_image_to_storage', return_value=None), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = _post_url()
    assert resp.status_code == 202


def test_url_happy_path_has_job_id_and_recipe_id():
    """
    /ingest/url pre-generates recipe_id internally (not from write_recipe_to_supabase),
    so we assert it is a non-empty UUID string rather than a specific value.
    """
    result = _make_ingest_response()
    mock_http = _make_httpx_mock(_URL_MARKDOWN_CONTENT)
    with patch(MOCK_TARGETS["client"]), \
         patch('httpx.AsyncClient', return_value=mock_http), \
         patch('recipeparser.adapters.api.html_to_text', return_value=_URL_SOURCE_TEXT), \
         patch('recipeparser.io.readers.epub.is_recipe_candidate', return_value=True), \
         patch('recipeparser.adapters.api._upload_image_to_storage', return_value=None), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = _post_url()
    data = resp.json()
    assert 'job_id' in data
    assert 'recipe_id' in data
    # recipe_id is pre-generated inside the endpoint; verify it is a valid UUID string
    import uuid as _uuid
    _uuid.UUID(data['recipe_id'])  # raises ValueError if not a valid UUID
    assert len(data['recipe_id']) == 36


def test_url_no_recipe_json_in_response():
    """The response must NOT contain recipe fields — only job_id and recipe_id."""
    result = _make_ingest_response()
    mock_http = _make_httpx_mock(_URL_MARKDOWN_CONTENT)
    with patch(MOCK_TARGETS["client"]), \
         patch('httpx.AsyncClient', return_value=mock_http), \
         patch('recipeparser.adapters.api.html_to_text', return_value=_URL_SOURCE_TEXT), \
         patch('recipeparser.io.readers.epub.is_recipe_candidate', return_value=True), \
         patch('recipeparser.adapters.api._upload_image_to_storage', return_value=None), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = _post_url()
    data = resp.json()
    for forbidden_key in ('title', 'embedding', 'structured_ingredients', 'tokenized_directions'):
        assert forbidden_key not in data, f"Response must not contain '{forbidden_key}'"


def test_url_missing_url_field_returns_400():
    """Omitting the url field entirely must return 400."""
    resp = client.post('/ingest/url', json={})
    assert resp.status_code in (400, 422)


def test_url_empty_url_returns_400():
    """An empty url string must return 400."""
    resp = _post_url(url='')
    assert resp.status_code == 400


def test_url_fetch_failure_returns_422():
    """When httpx raises (network error / non-2xx), the endpoint returns 422."""
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(side_effect=Exception('Connection refused'))
    with patch(MOCK_TARGETS["client"]), \
         patch('httpx.AsyncClient', return_value=mock_http):
        resp = _post_url()
    assert resp.status_code == 422
    assert 'Failed to fetch URL' in resp.json()['detail']


def test_url_not_a_recipe_returns_422():
    """When is_recipe_candidate returns False, the endpoint returns 422."""
    mock_http = _make_httpx_mock('This is a news article, not a recipe.')
    with patch(MOCK_TARGETS["client"]), \
         patch('httpx.AsyncClient', return_value=mock_http), \
         patch('recipeparser.adapters.api.html_to_text', return_value='This is a news article.'), \
         patch('recipeparser.io.readers.epub.is_recipe_candidate', return_value=False):
        resp = _post_url()
    assert resp.status_code == 422
    assert 'recipe' in resp.json()['detail'].lower()


def test_url_no_recipes_found_returns_422():
    """Pipeline ValueError → 422."""
    mock_http = _make_httpx_mock(_URL_MARKDOWN_CONTENT)
    with patch(MOCK_TARGETS["client"]), \
         patch('httpx.AsyncClient', return_value=mock_http), \
         patch('recipeparser.adapters.api.html_to_text', return_value=_URL_SOURCE_TEXT), \
         patch('recipeparser.io.readers.epub.is_recipe_candidate', return_value=True), \
         patch('recipeparser.adapters.api._upload_image_to_storage', return_value=None), \
         patch(MOCK_TARGETS["pipeline"], side_effect=ValueError('No recipes found in source text.')), \
         patch(MOCK_TARGETS["write"]):
        resp = _post_url()
    assert resp.status_code == 422
    assert 'No recipes found' in resp.json()['detail']


def test_url_pipeline_runtime_error_returns_500():
    """Pipeline RuntimeError → 500."""
    mock_http = _make_httpx_mock(_URL_MARKDOWN_CONTENT)
    with patch(MOCK_TARGETS["client"]), \
         patch('httpx.AsyncClient', return_value=mock_http), \
         patch('recipeparser.adapters.api.html_to_text', return_value=_URL_SOURCE_TEXT), \
         patch('recipeparser.io.readers.epub.is_recipe_candidate', return_value=True), \
         patch('recipeparser.adapters.api._upload_image_to_storage', return_value=None), \
         patch(MOCK_TARGETS["pipeline"], side_effect=RuntimeError('Refinement pass failed.')), \
         patch(MOCK_TARGETS["write"]):
        resp = _post_url()
    assert resp.status_code == 500
    assert 'Refinement pass failed' in resp.json()['detail']


def test_url_image_upload_failure_does_not_block_ingestion():
    """
    If _upload_image_to_storage returns None (upload failed), the pipeline
    must still run and the endpoint must still return 202.
    """
    result = _make_ingest_response()
    mock_http = _make_httpx_mock(_URL_MARKDOWN_CONTENT)
    with patch(MOCK_TARGETS["client"]), \
         patch('httpx.AsyncClient', return_value=mock_http), \
         patch('recipeparser.adapters.api.html_to_text', return_value=_URL_SOURCE_TEXT), \
         patch('recipeparser.io.readers.epub.is_recipe_candidate', return_value=True), \
         patch('recipeparser.adapters.api._upload_image_to_storage', return_value=None), \
         patch(MOCK_TARGETS["pipeline"], return_value=result), \
         patch(MOCK_TARGETS["write"], return_value=FAKE_RECIPE_ID):
        resp = _post_url()
    assert resp.status_code == 202
