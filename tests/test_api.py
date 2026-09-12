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

import asyncio
import io
import os
import time
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
    _fetch_page_meta,
    app,
)
from recipeparser.core.fsm import PipelineController, PipelineStatus  # noqa: E402
from recipeparser.io.readers.url import PageMeta  # noqa: E402
from recipeparser.io.writers.image_store import SupabaseImageStore  # noqa: E402

# ---------------------------------------------------------------------------
# Mock targets
# ---------------------------------------------------------------------------
# Phase 6: api.py instantiates RecipePipeline inline and hands it a JobSink
# (Task 6) so recipes are written as they land rather than after the batch.
# Patch the classes where they are imported (in the api module namespace).
_PIPELINE = "recipeparser.adapters.api.RecipePipeline"
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

def _patch_pipeline_and_writer() -> tuple[Any, Any, Any]:
    """Return context managers that patch RecipePipeline, SupabaseCategorySource,
    and _get_client so no real I/O occurs.

    Persistence is no longer a separate writer call: RecipePipeline.run() is
    handed a JobSink (Task 6) and writes recipes via its on_result callback as
    they land. Since RecipePipeline itself is mocked here, that callback is
    never actually invoked — tests that need to prove the wiring is live (not
    silently `None`, which was exactly the Task 6 regression) assert on the
    kwargs RecipePipeline.run() was called with instead.

    NOTE: invoking the real on_result/on_skip from here to prove persistence
    end-to-end was originally tried and abandoned, because `JobSink.__init__`'s
    `write` parameter used to default to `write_recipe_to_supabase` bound as a
    direct function-object reference at job_sink.py's *import* time (confirmed
    via `JobSink.__init__.__defaults__`) — patching that name afterwards, at
    either `recipeparser.io.writers.supabase.write_recipe_to_supabase` or
    `recipeparser.adapters.job_sink.write_recipe_to_supabase`, did not change
    the value already frozen into the default, so the real network-calling
    function still ran.

    That has since been fixed (whole-branch review, 2026-09-05): both endpoints
    now import `write_recipe_to_supabase` into api.py's own namespace and pass
    it explicitly — `JobSink(..., write=write_recipe_to_supabase)` — as a plain
    keyword argument evaluated each time `_run()` executes, not a def-time
    default. `recipeparser.adapters.api.write_recipe_to_supabase` is therefore
    the name to patch, and `test_the_patched_write_receives_the_recipe_end_to_end`
    below exercises the write end-to-end through it. (`JobSink.__init__`'s own
    default still freezes the same reference at job_sink.py's import time —
    harmless, since nothing outside test fakes constructs a `JobSink` without
    passing `write=` explicitly, but a reason to keep patching the api.py name
    rather than the job_sink.py default.)

    Asserting the identity/liveness of the callbacks RecipePipeline.run() was
    actually handed (`_assert_pipeline_run_wired_to_a_live_sink`) remains the
    right check for the tests that only care about wiring, not persistence.

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

    # SupabaseCategorySource mock: load_category_ids() returns empty dict
    mock_cat_src = stack.enter_context(_patch(_CAT_SRC))
    mock_cat_src.return_value.load_category_ids.return_value = {}

    return stack, mock_client, mock_pipeline_cls


def _assert_pipeline_run_wired_to_a_live_sink(mock_pipeline_cls: Any) -> None:
    """Assert RecipePipeline.run() was handed a real JobSink's callbacks.

    Stronger than `"on_result" in run_kwargs`, which is true even when the
    value is `None` — exactly the Task 6 regression (both endpoints passed
    `None` where these callbacks belong). `on_result.__self__` raises
    AttributeError on `None`, so this fails on the regression it guards.
    """
    run_kwargs = mock_pipeline_cls.return_value.run.call_args.kwargs
    assert run_kwargs["on_result"].__self__.__class__.__name__ == "JobSink"
    assert run_kwargs["on_skip"] is not None
    assert run_kwargs["on_progress"] is not None
    assert isinstance(mock_pipeline_cls.call_args.kwargs["image_store"], SupabaseImageStore)


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

    def test_results_are_written_via_the_sink(self) -> None:
        """RecipePipeline.run() must be handed a live on_result callback — the
        Task 6 regression was passing None there, so nothing was written until
        the whole batch finished. Also confirms the pipeline is constructed
        with a real SupabaseImageStore (Task 6), which is what finally lets
        photographs reach storage.

        NOTE: Uses TestClient as a context manager so the ASGI event loop
        drains the background task before assertions run.
        """
        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
        with stack, TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post("/jobs", json={"text": "Boil water. Add pasta."})
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        mock_pipeline_cls.assert_called_once()
        _assert_pipeline_run_wired_to_a_live_sink(mock_pipeline_cls)

    def test_the_patched_write_receives_the_recipe_end_to_end(self) -> None:
        """Persistence, proven end-to-end rather than by wiring inspection.

        RecipePipeline.run's mock now actually calls the on_result it was
        handed (JobSink.on_result), which calls the module-level
        `write_recipe_to_supabase` name in api.py's namespace. Patching that
        name — not JobSink.__init__'s frozen default — is what makes this
        interceptable; see the NOTE on `_patch_pipeline_and_writer` above.
        """
        recipe = _make_recipe()

        def _run_side_effect(_chunks: Any, **kwargs: Any) -> list[Any]:
            kwargs["on_result"](recipe)
            return [recipe]

        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
        mock_pipeline_cls.return_value.run.side_effect = _run_side_effect

        with stack, \
             patch("recipeparser.adapters.api.write_recipe_to_supabase") as mock_write, \
             TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post("/jobs", json={"text": "Boil water. Add pasta."})
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        mock_write.assert_called_once()
        assert mock_write.call_args.args[0] is recipe

    def test_a_url_job_takes_the_hero_and_the_description_from_the_page_meta(self) -> None:
        from unittest.mock import AsyncMock

        from recipeparser.io.readers.url import PageMeta

        markdown = (
            "Title: Noodles\n\n"
            "![Image 1](https://cooking.nytimes.com/_next/image?url=%2Fassets%2Fedamam-logo.png)\n\n"
            "1 cup noodles"
        )

        class _Resp:
            text = markdown

            def raise_for_status(self) -> None:
                return None

        class _Http:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self) -> "_Http":
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            async def get(self, url: str, **kw: Any) -> _Resp:
                return _Resp()

        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
        with stack, \
             patch("recipeparser.adapters.api.httpx.AsyncClient", _Http), \
             patch(
                 "recipeparser.adapters.api._fetch_page_meta",
                 new=AsyncMock(
                     return_value=PageMeta("https://static01.nyt.com/hero.jpg", "A weeknight noodle dish.")
                 ),
             ) as meta, \
             patch("recipeparser.adapters.api._upload_image_to_storage",
                   new=AsyncMock(return_value="https://storage.test/hero.jpg")) as upload, \
             TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post("/jobs", json={"url": "https://cooking.nytimes.com/recipes/1020732-noodles"})
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        meta.assert_awaited_once_with("https://cooking.nytimes.com/recipes/1020732-noodles")
        upload.assert_awaited_once()
        assert upload.await_args.args[0] == "https://static01.nyt.com/hero.jpg"   # the meta image, not the badge
        chunks = mock_pipeline_cls.return_value.run.call_args.args[0]
        assert chunks[0].image_url == "https://storage.test/hero.jpg"
        assert chunks[0].meta is not None and chunks[0].meta.description == "A weeknight noodle dish."

    def test_a_badge_og_image_does_not_win_and_does_not_suppress_the_markdown_photo(self) -> None:
        """A site-wide logo served AS the page's own og:image must not become
        the hero, and must not stop rule 3 from finding the markdown's real
        photograph."""
        from unittest.mock import AsyncMock

        from recipeparser.io.readers.url import PageMeta

        markdown = (
            "Title: Noodles\n\n"
            "![Dish](https://cdn.site.test/uploads/dish.jpg)\n\n"
            "1 cup noodles"
        )

        class _Resp:
            text = markdown

            def raise_for_status(self) -> None:
                return None

        class _Http:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self) -> "_Http":
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            async def get(self, url: str, **kw: Any) -> _Resp:
                return _Resp()

        badge_og_image = "https://cooking.nytimes.com/_next/image?url=%2Fassets%2Fedamam-logo.png"
        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
        with stack, \
             patch("recipeparser.adapters.api.httpx.AsyncClient", _Http), \
             patch(
                 "recipeparser.adapters.api._fetch_page_meta",
                 new=AsyncMock(
                     return_value=PageMeta(badge_og_image, "A weeknight noodle dish.")
                 ),
             ), \
             patch("recipeparser.adapters.api._upload_image_to_storage",
                   new=AsyncMock(return_value="https://storage.test/dish.jpg")) as upload, \
             TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post("/jobs", json={"url": "https://cooking.nytimes.com/recipes/1020732-noodles"})
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        upload.assert_awaited_once()
        assert upload.await_args.args[0] == "https://cdn.site.test/uploads/dish.jpg"  # the photo, not the badge

    def test_total_chunks_and_the_hint_land_before_the_pipeline_runs(self) -> None:
        order: list[str] = []
        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
        mock_pipeline_cls.return_value.run.side_effect = lambda *a, **kw: order.append("run") or []

        def _record(job_id: str, total: int, source_hint: "str | None" = None) -> None:
            order.append(f"total_chunks={total} hint={source_hint}")

        with stack, patch("recipeparser.adapters.api._update_total_chunks", side_effect=_record), \
             TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post("/jobs", json={"text": "Boil water. Add pasta."})
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        assert order == ["total_chunks=1 hint=None", "run"]


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

    def test_photo_returns_202_through_the_image_reader(self) -> None:
        mock_chunk = MagicMock()
        mock_chunk.text = "Cake\n1 cup flour\nMix."

        stack, mock_client, _mock_pipeline_cls = _patch_pipeline_and_writer()

        with stack, \
             patch("recipeparser.adapters.api._ImageReader") as mock_reader_cls, \
             TestClient(app, raise_server_exceptions=False) as tc:
            mock_reader_cls.return_value.read.return_value = [mock_chunk]
            resp = tc.post(
                "/jobs/file",
                files={"file": ("IMG_4021.jpg", io.BytesIO(b"\xff\xd8\xff\xe0"), "image/jpeg")},
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        mock_reader_cls.assert_called_once()          # built with the Gemini client
        assert mock_reader_cls.call_args.args[0] is mock_client.return_value

    def test_heic_is_a_422_with_the_sentence(self, client: TestClient) -> None:
        resp = self._upload(client, "IMG_1.heic", b"\x00\x00\x00\x18ftypheic", "image/heic")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "Cayenne can't read HEIC photos yet. Share it as a JPEG instead."

    def test_webp_is_a_422_with_the_sentence(self, client: TestClient) -> None:
        resp = self._upload(client, "page.webp", b"RIFF\x00\x00\x00\x00WEBP", "image/webp")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "Cayenne can't read WebP photos yet. Share it as a JPEG instead."

    def test_docx_is_a_422_naming_the_extension(self, client: TestClient) -> None:
        resp = self._upload(client, "menu.docx", b"PK\x03\x04", "application/octet-stream")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "Cayenne can't read .docx files yet."

    def test_paprikarecipes_flow_b_writes_pre_parsed_directly(self) -> None:
        """PAPRIKA_CAYENNE chunks (text="" + pre_parsed_embedding) must be routed
        through RecipePipeline which handles them via the cheap ASSEMBLE-only path
        ($0 — no Gemini calls). RecipePipeline.run() must be handed a *live*
        on_result callback so results are written as they land (Task 6). Merely
        checking "on_result" is a key in the call's kwargs would pass even when
        the value is None, which is exactly the regression this test exists to
        catch — see _assert_pipeline_run_wired_to_a_live_sink.

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

        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
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
            job_id = resp.json()["job_id"]

            # Poll until the background task completes: the task's finally-block
            # calls _active_jobs.pop(job_id), so absence means the run has finished.
            # Timeout after 5 s to avoid hanging CI on unexpected failures.
            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        # Assertions run after the context manager exits — patches are still
        # active above, so the mocks were used by the background task.
        # RecipePipeline must be instantiated (it handles Flow B routing internally)
        mock_pipeline_cls.assert_called_once()
        _assert_pipeline_run_wired_to_a_live_sink(mock_pipeline_cls)

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
        _active_jobs[job_id] = (os.environ["TEST_USER_ID"], controller)

        resp = client.get(f"/jobs/{job_id}")
        assert resp.status_code == 200

    def test_status_reflects_fsm_state(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        controller = PipelineController()
        controller.transition("start")  # IDLE → RUNNING
        _active_jobs[job_id] = (os.environ["TEST_USER_ID"], controller)

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
        _active_jobs[job_id] = (os.environ["TEST_USER_ID"], controller)
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
# Section 4b — GET /health
# ===========================================================================

class TestHealth:
    def test_reports_the_live_auth_mode(self, client: TestClient) -> None:
        """The mode has to be observable — a bypassed server otherwise looks
        identical to a verifying one until it misattributes a write."""
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # This suite deliberately runs with the bypass engaged.
        assert body["auth_mode"] == "bypassed"


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

    def test_a_badge_is_not_the_hero(self) -> None:
        md = (
            "# Recipe\n"
            "[Powered by ![Image 1](https://cooking.nytimes.com/_next/image?url=%2Fassets%2Fedamam-logo.png&w=768&q=75)](https://www.edamam.com/)\n"
            "Boil water."
        )
        assert _extract_image_url_from_markdown(md) is None

    def test_the_first_non_badge_markdown_image_wins(self) -> None:
        md = (
            "![Site logo](https://cdn.site.test/logo.png)\n"
            "![Spicy sesame noodles](https://cdn.site.test/uploads/dish.jpg)\n"
        )
        assert _extract_image_url_from_markdown(md) == "https://cdn.site.test/uploads/dish.jpg"

    def test_og_meta_line_still_beats_everything(self) -> None:
        md = "og:image: https://example.com/photo.jpg\n![Dish](https://example.com/other.jpg)"
        assert _extract_image_url_from_markdown(md) == "https://example.com/photo.jpg"


class TestFetchPageMeta:
    """_fetch_page_meta's three behaviours, offline: patch httpx.AsyncClient
    directly and drive the coroutine with asyncio.run — no endpoint, no job."""

    def test_an_html_response_is_parsed(self) -> None:
        html = (
            '<meta property="og:image" content="https://static01.nyt.com/hero.jpg">'
            '<meta name="description" content="A weeknight noodle dish.">'
        )

        class _Resp:
            text = html
            headers = {"content-type": "text/html; charset=utf-8"}

            def raise_for_status(self) -> None:
                return None

        class _Http:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self) -> "_Http":
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            async def get(self, url: str, **kw: Any) -> _Resp:
                return _Resp()

        with patch("recipeparser.adapters.api.httpx.AsyncClient", _Http):
            result = asyncio.run(_fetch_page_meta("https://example.com/recipe"))

        assert result == PageMeta("https://static01.nyt.com/hero.jpg", "A weeknight noodle dish.")

    def test_a_non_html_content_type_is_no_meta(self) -> None:
        class _Resp:
            text = "binary data, not read"
            headers = {"content-type": "image/jpeg"}

            def raise_for_status(self) -> None:
                return None

        class _Http:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self) -> "_Http":
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            async def get(self, url: str, **kw: Any) -> _Resp:
                return _Resp()

        with patch("recipeparser.adapters.api.httpx.AsyncClient", _Http):
            result = asyncio.run(_fetch_page_meta("https://example.com/photo.jpg"))

        assert result == PageMeta(None, None)

    def test_a_network_failure_is_no_meta_and_nothing_propagates(self) -> None:
        class _Http:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self) -> "_Http":
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            async def get(self, url: str, **kw: Any) -> Any:
                raise RuntimeError("boom")

        with patch("recipeparser.adapters.api.httpx.AsyncClient", _Http):
            result = asyncio.run(_fetch_page_meta("https://example.com/recipe"))

        assert result == PageMeta(None, None)

    def test_no_content_type_header_at_all_is_still_parsed(self) -> None:
        html = '<meta property="og:image" content="https://static01.nyt.com/hero.jpg">'

        class _Resp:
            text = html
            headers: dict = {}

            def raise_for_status(self) -> None:
                return None

        class _Http:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self) -> "_Http":
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            async def get(self, url: str, **kw: Any) -> _Resp:
                return _Resp()

        with patch("recipeparser.adapters.api.httpx.AsyncClient", _Http):
            result = asyncio.run(_fetch_page_meta("https://example.com/recipe"))

        assert result == PageMeta("https://static01.nyt.com/hero.jpg", None)


# ===========================================================================
# Cancellation reaches the terminal payload
# ===========================================================================

class TestCancelledJobsFinalizeAsCancelled:
    """A cancelled import used to report a clean success — the FSM was back at
    IDLE by finalize time. These prove the flag survives the whole round trip
    through the endpoint, not just through the controller."""

    def _finalized(self, monkeypatch: Any) -> dict[str, Any]:
        """Capture the single payload _finalize_ingestion_job is handed.

        Patched by dotted string rather than by importing the module: an
        `import recipeparser.adapters.api` here trips TID251, the hexagonal
        boundary rule, and the house rule is that TID251 is fixed rather than
        silenced with a noqa.
        """
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            "recipeparser.adapters.api._finalize_ingestion_job",
            lambda job_id, payload: captured.update(payload),
        )
        return captured

    def _drain(self, tc: TestClient, payload: dict[str, Any]) -> str:
        resp = tc.post("/jobs", json=payload)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        deadline = time.monotonic() + 5.0
        while job_id in _active_jobs and time.monotonic() < deadline:
            time.sleep(0.05)
        return job_id

    def test_a_cancelled_url_job_writes_status_cancelled(self, monkeypatch: Any) -> None:
        captured = self._finalized(monkeypatch)
        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()

        def _run_side_effect(_chunks: Any, **kwargs: Any) -> list[Any]:
            # Stand in for the user pressing Cancel mid-run: the controller for
            # this job is the only one in the registry at this point.
            for _user_id, controller in list(_active_jobs.values()):
                controller.transition("start")
                controller.request_cancel()
                controller.transition("done")
            return []

        mock_pipeline_cls.return_value.run.side_effect = _run_side_effect

        with stack, TestClient(app, raise_server_exceptions=False) as tc:
            self._drain(tc, {"text": "Boil water. Add pasta."})

        assert captured["status"] == "cancelled"
        assert captured["stage"] == "DONE"
        assert "progress_pct" not in captured

    def test_an_uncancelled_url_job_still_writes_done(self, monkeypatch: Any) -> None:
        captured = self._finalized(monkeypatch)
        stack, _mock_client, _mock_pipeline_cls = _patch_pipeline_and_writer()

        with stack, TestClient(app, raise_server_exceptions=False) as tc:
            self._drain(tc, {"text": "Boil water. Add pasta."})

        assert captured["status"] == "done"
        assert captured["progress_pct"] == 100


# ===========================================================================
# total_chunks — the denominator, written after the reader returns
# ===========================================================================

class TestTotalChunks:

    def _captured(self, monkeypatch: Any) -> list[tuple[str, int, Any]]:
        calls: list[tuple[str, int, Any]] = []
        monkeypatch.setattr(
            "recipeparser.adapters.api._update_total_chunks",
            lambda job_id, total, source_hint=None: calls.append((job_id, total, source_hint)),
        )
        return calls

    def _drain(self, tc: TestClient, job_id: str) -> None:
        deadline = time.monotonic() + 5.0
        while job_id in _active_jobs and time.monotonic() < deadline:
            time.sleep(0.05)

    def test_a_url_job_records_one_chunk(self, monkeypatch: Any) -> None:
        calls = self._captured(monkeypatch)
        stack, _mock_client, _mock_pipeline_cls = _patch_pipeline_and_writer()

        with stack, TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post("/jobs", json={"text": "Boil water. Add pasta."})
            job_id = resp.json()["job_id"]
            self._drain(tc, job_id)

        assert calls == [(job_id, 1, None)]  # plain text carries no citation to hint from

    def test_a_file_job_records_what_the_reader_returned(self, monkeypatch: Any) -> None:
        calls = self._captured(monkeypatch)
        chunks = [MagicMock() for _ in range(3)]
        for chunk in chunks:
            chunk.text = "pasta"
        stack, _mock_client, _mock_pipeline_cls = _patch_pipeline_and_writer()

        with stack, \
             patch("recipeparser.adapters.api._PdfReader") as mock_reader_cls, \
             TestClient(app, raise_server_exceptions=False) as tc:
            mock_reader_cls.return_value.read.return_value = chunks
            resp = tc.post(
                "/jobs/file",
                files={"file": ("recipe.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            )
            job_id = resp.json()["job_id"]
            self._drain(tc, job_id)

        # Each mock chunk carries its own auto-generated (distinct) citation
        # mock; the tie is broken by first-seen, so chunks[0]'s wins.
        assert calls == [(job_id, 3, chunks[0].citation.key)]
