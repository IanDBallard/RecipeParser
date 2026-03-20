"""tests/test_api.py — Phase 6 API tests (canonical).

Covers:
  POST /jobs              — URL/text fire-and-forget
  POST /jobs/file         — file upload fire-and-forget
  GET  /jobs/{job_id}     — status polling
  POST /jobs/{job_id}/pause|resume|cancel — control endpoints
  POST /embed             — embedding generation
  _extract_image_url_from_markdown — pure-function unit tests

No legacy /ingest* tests — those endpoints were removed in Phase 6.
"""
from __future__ import annotations

import io
import os
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Auth bypass — must be set BEFORE importing the app so the module-level
# _DISABLE_AUTH flag is True when the app is constructed.
# ---------------------------------------------------------------------------
os.environ.setdefault("DISABLE_AUTH", "1")
os.environ.setdefault("TEST_USER_ID", "00000000-0000-0000-0000-000000000001")
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")

from recipeparser.adapters.api import (  # noqa: E402
    _active_jobs,
    _extract_image_url_from_markdown,
    app,
)
from recipeparser.core.fsm import PipelineController, PipelineStatus  # noqa: E402

# ---------------------------------------------------------------------------
# Mock targets
# ---------------------------------------------------------------------------
# Phase 6: api.py instantiates RecipePipeline and SupabaseWriter inline.
# Patch the classes where they are imported (in the api module namespace).
_PIPELINE = "recipeparser.adapters.api.RecipePipeline"
_WRITE    = "recipeparser.adapters.api.SupabaseWriter"
_CLIENT   = "recipeparser.adapters.api._get_client"
_EMBED    = "recipeparser.gemini.get_embeddings"
# Also patch the category source so it doesn't hit Supabase in tests.
_CAT_SRC  = "recipeparser.adapters.api.SupabaseCategorySource"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client() -> TestClient:
    """Return a synchronous TestClient for the FastAPI app."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clear_active_jobs() -> Any:
    """Ensure _active_jobs is empty before and after every test."""
    _active_jobs.clear()
    yield
    _active_jobs.clear()


def _make_recipe() -> MagicMock:
    """Return a minimal mock CayenneRecipe."""
    r = MagicMock()
    r.title = "Test Recipe"
    return r


# ===========================================================================
# Section 1 — POST /jobs
# ===========================================================================

def _patch_pipeline_and_writer() -> tuple[Any, Any, Any, Any]:
    """Return context managers that patch RecipePipeline, SupabaseWriter,
    SupabaseCategorySource, and _get_client so no real I/O occurs.

    Usage::

        with _patch_pipeline_and_writer():
            resp = client.post("/jobs", ...)
    """
    from contextlib import ExitStack
    from unittest.mock import patch as _patch

    stack = ExitStack()

    mock_client = stack.enter_context(_patch(_CLIENT, return_value=MagicMock()))

    # RecipePipeline mock: instance.run() returns a list with one recipe
    mock_pipeline_cls = stack.enter_context(_patch(_PIPELINE))
    mock_pipeline_cls.return_value.run.return_value = [_make_recipe()]

    # SupabaseWriter mock: instance.write() is a no-op
    mock_writer_cls = stack.enter_context(_patch(_WRITE))
    mock_writer_cls.return_value.write.return_value = None

    # SupabaseCategorySource mock: load_category_ids() returns empty dict
    mock_cat_src = stack.enter_context(_patch(_CAT_SRC))
    mock_cat_src.return_value.load_category_ids.return_value = {}

    return stack, mock_client, mock_pipeline_cls, mock_writer_cls


class TestPostJobs:
    def test_missing_url_and_text_returns_400(self, client: TestClient) -> None:
        resp = client.post("/jobs", json={})
        assert resp.status_code == 400
        assert "url" in resp.json()["detail"].lower() or "text" in resp.json()["detail"].lower()

    def test_text_returns_202(self, client: TestClient) -> None:
        with _patch_pipeline_and_writer()[0]:
            resp = client.post("/jobs", json={"text": "Boil water. Add pasta."})
        assert resp.status_code == 202

    def test_response_has_only_job_id(self, client: TestClient) -> None:
        with _patch_pipeline_and_writer()[0]:
            resp = client.post("/jobs", json={"text": "Boil water."})
        body = resp.json()
        assert set(body.keys()) == {"job_id"}
        # job_id should be a valid UUID string
        uuid.UUID(body["job_id"])  # raises ValueError if invalid

    def test_url_returns_202(self, client: TestClient) -> None:
        mock_http_resp = MagicMock()
        mock_http_resp.text = "# Pasta\nog:image: https://example.com/img.jpg\nBoil water."
        mock_http_resp.raise_for_status = MagicMock()

        with _patch_pipeline_and_writer()[0], \
             patch("httpx.AsyncClient") as mock_httpx:
            mock_httpx.return_value.__aenter__.return_value.get = \
                MagicMock(return_value=mock_http_resp)
            resp = client.post("/jobs", json={"url": "https://example.com/recipe"})
        assert resp.status_code == 202


# ===========================================================================
# Section 2 — POST /jobs/file
# ===========================================================================

class TestPostJobsFile:
    def _upload(
        self,
        client: TestClient,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> Any:
        return client.post(
            "/jobs/file",
            files={"file": (filename, io.BytesIO(content), content_type)},
        )

    def test_unsupported_type_returns_422(self, client: TestClient) -> None:
        resp = self._upload(client, "recipe.txt", b"hello", "text/plain")
        assert resp.status_code == 422

    def test_pdf_returns_202(self, client: TestClient) -> None:
        with _patch_pipeline_and_writer()[0], \
             patch("recipeparser.io.readers.pdf.extract_text_from_pdf", return_value="pasta"):
            resp = self._upload(client, "recipe.pdf", b"%PDF-1.4", "application/pdf")
        assert resp.status_code == 202

    def test_paprikarecipes_returns_202(self, client: TestClient) -> None:
        # Build a minimal mock chunk with non-empty text (PAPRIKA_LEGACY)
        mock_chunk = MagicMock()
        mock_chunk.text = "Pasta\n\nIngredients:\n1 cup pasta\n\nDirections:\nBoil."

        with _patch_pipeline_and_writer()[0], \
             patch("recipeparser.adapters.api._PaprikaReader") as mock_reader_cls:
            mock_reader_cls.return_value.read.return_value = [mock_chunk]
            resp = self._upload(
                client,
                "recipes.paprikarecipes",
                b"PK\x03\x04",  # minimal ZIP magic bytes
                "application/octet-stream",
            )
        assert resp.status_code == 202

    def test_epub_returns_202(self, client: TestClient) -> None:
        with _patch_pipeline_and_writer()[0], \
             patch("recipeparser.io.readers.epub.extract_text_from_epub", return_value="pasta recipe"):
            resp = self._upload(client, "cookbook.epub", b"PK\x03\x04", "application/epub+zip")
        assert resp.status_code == 202

    def test_paprikarecipes_flow_b_writes_pre_parsed_directly(self) -> None:
        """PAPRIKA_CAYENNE chunks (text="" + pre_parsed_embedding) must be routed
        through RecipePipeline which handles them via the cheap ASSEMBLE-only path
        ($0 — no Gemini calls).  SupabaseWriter.write() must be called with the result.

        In the Phase 6 architecture, RecipePipeline._get_stages() routes
        PAPRIKA_CAYENNE chunks internally — the pipeline IS instantiated, but
        it skips EXTRACT/REFINE/EMBED and goes straight to ASSEMBLE.

        NOTE: Uses TestClient as a context manager to ensure the ASGI event loop
        drains all background tasks (asyncio.create_task) before assertions run.
        Patches must remain active for the full duration including background task
        execution — exiting the patch context before the task runs causes the real
        classes to be used, which fail silently.
        """
        # Build a PAPRIKA_CAYENNE chunk: text="" + pre_parsed CayenneRecipe + embedding
        mock_pre_parsed = MagicMock()
        mock_pre_parsed.model_dump.return_value = {
            "title": "Cayenne Pasta",
            "prep_time": "10 min",
            "cook_time": "20 min",
            "base_servings": 4.0,
            "source_url": None,
            "categories": ["Italian"],
            "structured_ingredients": [],
            "tokenized_directions": [],
        }
        mock_chunk = MagicMock()
        mock_chunk.text = ""  # PAPRIKA_CAYENNE — no text
        mock_chunk.pre_parsed = mock_pre_parsed
        mock_chunk.pre_parsed_embedding = [0.1] * 1536

        stack, _mock_client, mock_pipeline_cls, mock_writer_cls = _patch_pipeline_and_writer()
        # Use TestClient as a context manager so the ASGI event loop is fully
        # drained (all asyncio.create_task background tasks complete) before
        # the with-block exits and we assert on the mocks.
        with stack, \
             patch("recipeparser.adapters.api._PaprikaReader") as mock_reader_cls, \
             TestClient(app, raise_server_exceptions=False) as tc:
            mock_reader_cls.return_value.read.return_value = [mock_chunk]
            resp = tc.post(
                "/jobs/file",
                files={"file": ("cayenne_export.paprikarecipes", io.BytesIO(b"PK\x03\x04"), "application/octet-stream")},
            )
            assert resp.status_code == 202

        # Assertions run after the context manager exits — by then the ASGI
        # lifespan has shut down and all background tasks have completed.
        # RecipePipeline must be instantiated (it handles Flow B routing internally)
        mock_pipeline_cls.assert_called_once()
        # SupabaseWriter.write() must be called exactly once with the pipeline results
        mock_writer_cls.return_value.write.assert_called_once()

    def test_file_response_has_only_job_id(self, client: TestClient) -> None:
        with _patch_pipeline_and_writer()[0], \
             patch("recipeparser.io.readers.pdf.extract_text_from_pdf", return_value="pasta"):
            resp = self._upload(client, "recipe.pdf", b"%PDF-1.4", "application/pdf")
        body = resp.json()
        assert set(body.keys()) == {"job_id"}
        uuid.UUID(body["job_id"])


# ===========================================================================
# Section 3 — GET /jobs/{job_id}
# ===========================================================================

class TestGetJobStatus:
    def test_unknown_job_returns_404(self, client: TestClient) -> None:
        resp = client.get("/jobs/nonexistent-job-id")
        assert resp.status_code == 404

    def test_known_job_returns_200(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        controller = PipelineController()
        controller.transition("start")  # IDLE → RUNNING
        _active_jobs[job_id] = controller

        resp = client.get(f"/jobs/{job_id}")
        assert resp.status_code == 200

    def test_status_reflects_fsm_state(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        controller = PipelineController()
        controller.transition("start")  # IDLE → RUNNING
        _active_jobs[job_id] = controller

        resp = client.get(f"/jobs/{job_id}")
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["status"] == PipelineStatus.RUNNING.value  # "running"


# ===========================================================================
# Section 4 — Control endpoints (pause / resume / cancel)
# ===========================================================================

class TestControlEndpoints:
    def _running_controller(self) -> tuple[str, PipelineController]:
        job_id = str(uuid.uuid4())
        controller = PipelineController()
        controller.transition("start")  # IDLE → RUNNING
        _active_jobs[job_id] = controller
        return job_id, controller

    # ── pause ────────────────────────────────────────────────────────────────

    def test_pause_unknown_job_returns_404(self, client: TestClient) -> None:
        resp = client.post("/jobs/no-such-job/pause")
        assert resp.status_code == 404

    def test_pause_running_job_transitions_to_pausing(self, client: TestClient) -> None:
        job_id, controller = self._running_controller()
        resp = client.post(f"/jobs/{job_id}/pause")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["status"] == PipelineStatus.PAUSING.value  # "pausing"
        assert controller.status == PipelineStatus.PAUSING

    # ── cancel ───────────────────────────────────────────────────────────────

    def test_cancel_running_job_transitions_to_cancelling(self, client: TestClient) -> None:
        job_id, controller = self._running_controller()
        resp = client.post(f"/jobs/{job_id}/cancel")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == PipelineStatus.CANCELLING.value  # "cancelling"
        assert controller.status == PipelineStatus.CANCELLING

    def test_cancel_unknown_job_returns_404(self, client: TestClient) -> None:
        resp = client.post("/jobs/no-such-job/cancel")
        assert resp.status_code == 404

    # ── resume ───────────────────────────────────────────────────────────────

    def test_resume_unknown_job_returns_404(self, client: TestClient) -> None:
        resp = client.post("/jobs/no-such-job/resume")
        assert resp.status_code == 404

    def test_resume_paused_job_transitions_to_resuming(self, client: TestClient) -> None:
        job_id, controller = self._running_controller()
        # Manually drive to PAUSED state
        controller.transition("pause")   # RUNNING → PAUSING
        controller.transition("paused")  # PAUSING → PAUSED

        resp = client.post(f"/jobs/{job_id}/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == PipelineStatus.RESUMING.value  # "resuming"
        assert controller.status == PipelineStatus.RESUMING


# ===========================================================================
# Section 5 — POST /embed
# ===========================================================================

class TestEmbed:
    def test_returns_200_with_embedding(self, client: TestClient) -> None:
        fake_embedding = [0.1] * 1536
        with patch(_CLIENT, return_value=MagicMock()), \
             patch(_EMBED, return_value=fake_embedding):
            resp = client.post("/embed", json={"text": "chocolate cake"})
        assert resp.status_code == 200
        body = resp.json()
        assert "embedding" in body
        assert len(body["embedding"]) == 1536
        assert body["embedding"][0] == pytest.approx(0.1)

    def test_returns_500_on_error(self, client: TestClient) -> None:
        with patch(_CLIENT, side_effect=RuntimeError("GOOGLE_API_KEY not found")):
            resp = client.post("/embed", json={"text": "anything"})
        assert resp.status_code == 500


# ===========================================================================
# Section 6 — Pure function: _extract_image_url_from_markdown
# ===========================================================================

class TestExtractImageUrl:
    def test_extract_og_image(self) -> None:
        md = "og:image: https://example.com/photo.jpg\nSome text"
        assert _extract_image_url_from_markdown(md) == "https://example.com/photo.jpg"

    def test_extract_twitter_image(self) -> None:
        md = "twitter:image: https://cdn.example.com/img.png\nSome text"
        assert _extract_image_url_from_markdown(md) == "https://cdn.example.com/img.png"

    def test_extract_markdown_image(self) -> None:
        md = "# Recipe\n![A tasty dish](https://example.com/dish.jpg)\nBoil water."
        assert _extract_image_url_from_markdown(md) == "https://example.com/dish.jpg"

    def test_no_image_returns_none(self) -> None:
        md = "# Recipe\nBoil water. Add pasta."
        assert _extract_image_url_from_markdown(md) is None

    def test_og_image_cleans_double_paren(self) -> None:
        # Some Jina responses wrap the URL in an extra closing paren
        md = "og:image: https://example.com/photo.jpg))\nSome text"
        result = _extract_image_url_from_markdown(md)
        assert result is not None
        assert not result.endswith("))")
        assert result.endswith(")")
