"""FastAPI application for the Cayenne Ingestion API.

Endpoints (canonical — Phase 6):
  POST /jobs              — fire-and-forget URL/text job (returns 202 + { job_id })
  POST /jobs/file         — fire-and-forget file upload job (returns 202 + { job_id })
  GET  /jobs/{job_id}     — poll job status from _active_jobs registry
  POST /jobs/{job_id}/pause   — pause a running job
  POST /jobs/{job_id}/resume  — resume a paused job
  POST /jobs/{job_id}/cancel  — cancel a job
  POST /embed             — generate a 1536-dim embedding (returns 200 + {embedding})

Auth:
  HTTPBearer JWT verified against Supabase.
  Set DISABLE_AUTH=1 + TEST_USER_ID=<uuid> to bypass in tests.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import uuid
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType
from recipeparser.core.pipeline import RecipePipeline
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource
from recipeparser.io.readers.epub import EpubReader as _EpubReader
from recipeparser.io.readers.paprika import PaprikaReader as _PaprikaReader
from recipeparser.io.readers.pdf import PdfReader as _PdfReader
from recipeparser.io.writers.supabase import SupabaseWriter
import recipeparser.gemini as _gemini_mod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App + auth setup
# ---------------------------------------------------------------------------

_DISABLE_AUTH = os.environ.get("DISABLE_AUTH", "0") == "1"
_bearer = HTTPBearer(auto_error=not _DISABLE_AUTH)

app = FastAPI(title="Cayenne Ingestion API", version="1.0.0")


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _verify_supabase_jwt(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict[str, Any]:
    """Verify a Supabase-issued JWT and return the decoded payload.

    When DISABLE_AUTH=1 (test mode) the token is not verified and the
    TEST_USER_ID env var is used as the subject claim.
    """
    if _DISABLE_AUTH:
        user_id = os.environ.get("TEST_USER_ID", "test-user")
        return {"sub": user_id}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    token = credentials.credentials
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    try:
        import jwt as pyjwt  # noqa: PLC0415

        jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
        jwks_client = pyjwt.PyJWKClient(jwks_url)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload: dict[str, Any] = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience="authenticated",
        )
        return payload
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Client factory (mockable)
# ---------------------------------------------------------------------------

def _get_client() -> Any:
    """Create and return a Gemini generative client.

    Raises RuntimeError if GOOGLE_API_KEY is not set.
    This function is a named top-level so tests can patch it via
    ``recipeparser.adapters.api._get_client``.
    """
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not found in environment")
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    return genai.GenerativeModel("gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _extract_image_url_from_markdown(md: str) -> Optional[str]:
    """Extract the best image URL from Jina-flavoured markdown.

    Priority:
      1. ``og:image: <url>`` meta line
      2. ``twitter:image: <url>`` meta line
      3. First Markdown image ``![alt](url)``

    A trailing ``))`` is cleaned to a single ``)``.
    """
    # 1 & 2 — og/twitter meta lines
    meta_match = re.search(
        r"(?:og|twitter):image:\s*(https?://\S+)", md
    )
    if meta_match:
        url = meta_match.group(1)
        if url.endswith("))"):
            url = url[:-1]
        return url

    # 3 — first Markdown image tag
    md_match = re.search(r"!\[[^\]]*\]\((https?://[^)]+)\)", md)
    if md_match:
        return md_match.group(1)

    return None


def html_to_text(markdown: str) -> str:
    """Strip Markdown formatting to produce plain text for the pipeline.

    Removes:
      - Markdown image tags ``![alt](url)``
      - Inline links ``[text](url)`` → ``text``
      - Heading markers ``#``
      - Bold/italic markers ``**`` / ``*`` / ``__`` / ``_``
      - Meta lines (``og:image:``, ``twitter:image:``)
    """
    text = markdown
    # Remove meta lines
    text = re.sub(r"(?:og|twitter):\S+:.*\n?", "", text)
    # Remove image tags entirely
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # Convert links to their display text
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # Remove heading markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove bold/italic markers
    text = re.sub(r"\*{1,2}|_{1,2}", "", text)
    return text.strip()


async def _upload_image_to_storage(image_url: str, recipe_id: str) -> Optional[str]:
    """Download *image_url* and upload it to Supabase Storage.

    Returns the public storage URL on success, or ``None`` on any failure
    (network error, storage error, etc.).  Failures are logged but never
    propagate — image upload must never block recipe ingestion.
    """
    try:
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not supabase_url or not supabase_key:
            logger.warning("Supabase credentials not set; skipping image upload")
            return None

        async with httpx.AsyncClient(timeout=15) as http:
            img_resp = await http.get(image_url)
            img_resp.raise_for_status()
            img_bytes = img_resp.content
            content_type = img_resp.headers.get("content-type", "image/jpeg")

        # Derive a simple extension from content-type
        ext = content_type.split("/")[-1].split(";")[0].strip() or "jpg"
        storage_path = f"recipe-images/{recipe_id}.{ext}"

        from supabase import create_client  # type: ignore[import-not-found]
        sb = create_client(supabase_url, supabase_key)
        sb.storage.from_("recipe-images").upload(
            storage_path,
            img_bytes,
            {"content-type": content_type, "upsert": "true"},
        )
        public_url: str = sb.storage.from_("recipe-images").get_public_url(storage_path)
        return public_url
    except Exception as exc:
        logger.warning("Image upload failed for %s: %s", image_url, exc)
        return None


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class EmbedRequest(BaseModel):
    text: str


class AsyncJobResponse(BaseModel):
    """Returned immediately by POST /jobs and POST /jobs/file (fire-and-forget)."""
    job_id: str


class JobStatusResponse(BaseModel):
    """Returned by GET /jobs/{job_id}."""
    job_id: str
    status: str   # PipelineStatus.value string


class EmbedResponse(BaseModel):
    embedding: list[float]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/embed", response_model=EmbedResponse, status_code=200)
def embed_text(
    body: EmbedRequest,
    user: dict[str, Any] = Depends(_verify_supabase_jwt),
) -> EmbedResponse:
    """Generate a 1536-dim embedding for the given text."""
    try:
        client = _get_client()  # validates API key is present
        embedding = _gemini_mod.get_embeddings(body.text, client)
        return EmbedResponse(embedding=embedding)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ===========================================================================
# Phase 6 — Canonical fire-and-forget endpoints
# ===========================================================================

def _make_stage_callback(job_id: str) -> Callable[[str], None]:
    """
    Return a synchronous ``StageChangeCallback`` that writes the new stage
    name to ``ingestion_jobs.stage`` in Supabase.

    Called from a ThreadPoolExecutor worker thread (pipeline.run runs in
    asyncio.to_thread), so we use the synchronous supabase-py client.

    Failures are logged but **re-raised** (§11.4 — FAIL LOUDLY): a silent
    failure here leaves the Cayenne app showing a stale stage label, which
    is indistinguishable from a zombie job.
    """
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    def _on_stage_change(stage: str) -> None:
        if not supabase_url or not supabase_key:
            logger.warning(
                "Job %s: Supabase credentials not set — cannot update stage to '%s'.",
                job_id, stage,
            )
            return
        try:
            from supabase import create_client  # type: ignore[import-not-found]
            sb = create_client(supabase_url, supabase_key)
            sb.table("ingestion_jobs").update({"stage": stage}).eq("id", job_id).execute()
            logger.debug("Job %s: stage → %s", job_id, stage)
        except Exception:
            logger.exception(
                "Job %s: failed to update ingestion_jobs.stage to '%s' — re-raising (§11.4).",
                job_id, stage,
            )
            raise

    return _on_stage_change


# Process-level registry: job_id → PipelineController
# Allows pause/resume/cancel from the control endpoints.
_active_jobs: Dict[str, PipelineController] = {}


# ---------------------------------------------------------------------------
# Phase 6 helpers
# ---------------------------------------------------------------------------

def _select_reader(filename: str, content_type: str) -> str:
    """Return a reader tag string based on filename extension (primary) or content-type.

    Returns one of: 'pdf', 'epub', 'paprika'.
    Raises ValueError for unsupported types (caller converts to 422).
    """
    ext = Path(filename).suffix.lower()
    if ext == ".pdf" or content_type == "application/pdf":
        return "pdf"
    if ext == ".epub" or content_type == "application/epub+zip":
        return "epub"
    if ext == ".paprikarecipes":
        return "paprika"
    raise ValueError(
        f"Unsupported file type: extension='{ext}', content_type='{content_type}'."
    )


# ---------------------------------------------------------------------------
# POST /jobs  — canonical URL/text fire-and-forget endpoint
# ---------------------------------------------------------------------------

class JobsRequest(BaseModel):
    """Request body for POST /jobs."""
    url: Optional[str] = None
    text: Optional[str] = None
    uom_system: str = "US"
    measure_preference: str = "Volume"


@app.post("/jobs", response_model=AsyncJobResponse, status_code=202)
async def submit_job(
    body: JobsRequest,
    user: dict[str, Any] = Depends(_verify_supabase_jwt),
) -> AsyncJobResponse:
    """Fire-and-forget URL or text ingestion job.

    Returns 202 + ``{ job_id }`` immediately.  The pipeline runs in the
    background via ``asyncio.to_thread()``.  Recipes appear in the app
    automatically when PowerSync syncs the new ``recipes`` row written by
    the pipeline.
    """
    if not body.url and not body.text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'url' or 'text' must be provided.",
        )

    user_id: str = user.get("sub", "")
    job_id = str(uuid.uuid4())
    controller = PipelineController(on_stage_change=_make_stage_callback(job_id))
    _active_jobs[job_id] = controller

    async def _run() -> None:
        try:
            client = _get_client()
            source_text: str
            source_url: Optional[str] = None
            stored_image_url: Optional[str] = None

            if body.url:
                source_url = body.url
                jina_url = f"https://r.jina.ai/{body.url}"
                async with httpx.AsyncClient(timeout=30) as http:
                    resp = await http.get(jina_url)
                    resp.raise_for_status()
                    markdown_text = resp.text
                image_url_candidate = _extract_image_url_from_markdown(markdown_text)
                recipe_id_for_img = str(uuid.uuid4())
                if image_url_candidate:
                    stored_image_url = await _upload_image_to_storage(
                        image_url_candidate, recipe_id_for_img
                    )
                source_text = html_to_text(markdown_text)
            else:
                source_text = (body.text or "").strip()

            # Build a single URL/text chunk for the pipeline.
            # Both URL-scraped and raw-text paths use InputType.URL so the
            # pipeline routes them through the full EXTRACT→REFINE→…→ASSEMBLE
            # sequence.  source_url is None for raw-text submissions.
            chunk = Chunk(
                text=source_text,
                input_type=InputType.URL,
                source_url=source_url,
                image_url=stored_image_url,
            )

            # Wire category source + writer
            category_source = SupabaseCategorySource()
            category_ids = category_source.load_category_ids(user_id)
            writer = SupabaseWriter(user_id=user_id, category_ids=category_ids)

            pipeline = RecipePipeline(
                client=client,
                controller=controller,
                category_source=category_source,
                uom_system=body.uom_system,
                measure_preference=body.measure_preference,
            )
            results = await asyncio.to_thread(pipeline.run, [chunk], None, user_id)
            await asyncio.to_thread(writer.write, results)
            logger.info("Job %s completed successfully (%d recipe(s)).", job_id, len(results))
        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
            controller.transition("error")
        finally:
            _active_jobs.pop(job_id, None)

    asyncio.create_task(_run())
    return AsyncJobResponse(job_id=job_id)


# ---------------------------------------------------------------------------
# POST /jobs/file  — canonical file upload fire-and-forget endpoint
# ---------------------------------------------------------------------------

@app.post("/jobs/file", response_model=AsyncJobResponse, status_code=202)
async def submit_file_job(
    file: UploadFile = File(...),
    uom_system: str = "US",
    measure_preference: str = "Volume",
    user: dict[str, Any] = Depends(_verify_supabase_jwt),
) -> AsyncJobResponse:
    """Fire-and-forget file upload ingestion job.

    Accepts PDF, EPUB, or .paprikarecipes files.  Routes to the correct
    reader via ``_select_reader()``.  Returns 202 + ``{ job_id }`` immediately.
    """
    filename = file.filename or ""
    content_type = file.content_type or ""

    try:
        reader_tag = _select_reader(filename, content_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    file_bytes = await file.read()
    user_id: str = user.get("sub", "")
    job_id = str(uuid.uuid4())
    controller = PipelineController(on_stage_change=_make_stage_callback(job_id))
    _active_jobs[job_id] = controller

    async def _run() -> None:
        # NOTE: Do NOT call controller.transition("start") here.
        # RecipePipeline.run() calls it internally (IDLE → RUNNING).
        # Calling it here first would cause an invalid double-transition.
        try:
            client = _get_client()

            # Write bytes to a temp file (readers expect a filesystem path)
            suffix = Path(filename).suffix or ".bin"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name

            try:
                # Use the appropriate reader to produce List[Chunk].
                # The pipeline's stage router (_get_stages) inspects each
                # chunk's input_type and routes accordingly:
                #   PDF / EPUB          → full pipeline (EXTRACT→…→ASSEMBLE)
                #   PAPRIKA_LEGACY      → full pipeline (EXTRACT→…→ASSEMBLE)
                #   PAPRIKA_CAYENNE + embedding  → ASSEMBLE only ($0)
                #   PAPRIKA_CAYENNE no embedding → EMBED + ASSEMBLE (1 call)
                if reader_tag == "pdf":
                    chunks = await asyncio.to_thread(_PdfReader().read, tmp_path)
                elif reader_tag == "epub":
                    chunks = await asyncio.to_thread(_EpubReader().read, tmp_path)
                else:  # paprika
                    chunks = await asyncio.to_thread(_PaprikaReader().read, tmp_path)
            finally:
                os.unlink(tmp_path)

            # Wire category source + writer
            category_source = SupabaseCategorySource()
            category_ids = category_source.load_category_ids(user_id)
            writer = SupabaseWriter(user_id=user_id, category_ids=category_ids)

            # RecipePipeline.run() transitions IDLE→RUNNING internally,
            # processes all chunks (with per-chunk error isolation), then
            # transitions RUNNING→IDLE on success.
            pipeline = RecipePipeline(
                client=client,
                controller=controller,
                category_source=category_source,
                uom_system=uom_system,
                measure_preference=measure_preference,
            )
            results = await asyncio.to_thread(pipeline.run, chunks, None, user_id)
            await asyncio.to_thread(writer.write, results)
            logger.info(
                "File job %s completed successfully (%d recipe(s)).",
                job_id, len(results),
            )
        except Exception as exc:
            logger.error("File job %s failed: %s", job_id, exc, exc_info=True)
            # Transition to IDLE via "error" event.  The FSM allows this from
            # RUNNING, PAUSING, and RESUMING states.  If the pipeline never
            # started (e.g. reader raised before pipeline.run()), the controller
            # is still IDLE and the transition is a no-op (logs a warning).
            controller.transition("error")
        finally:
            _active_jobs.pop(job_id, None)

    asyncio.create_task(_run())
    return AsyncJobResponse(job_id=job_id)


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}  — status polling
# ---------------------------------------------------------------------------

@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    """Return the current FSM status of a running job.

    Returns 404 if the job is not in the active registry (already completed
    or never existed).
    """
    controller = _active_jobs.get(job_id)
    if controller is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    return JobStatusResponse(job_id=job_id, status=controller.status.value)


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/pause|resume|cancel  — control endpoints
# ---------------------------------------------------------------------------

@app.post("/jobs/{job_id}/pause", status_code=200)
def pause_job(job_id: str) -> dict[str, str]:
    """Request a pause on a running job."""
    controller = _active_jobs.get(job_id)
    if controller is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    controller.request_pause()
    return {"job_id": job_id, "status": controller.status.value}


@app.post("/jobs/{job_id}/resume", status_code=200)
def resume_job(job_id: str) -> dict[str, str]:
    """Resume a paused job."""
    controller = _active_jobs.get(job_id)
    if controller is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    controller.request_resume()
    return {"job_id": job_id, "status": controller.status.value}


@app.post("/jobs/{job_id}/cancel", status_code=200)
def cancel_job(job_id: str) -> dict[str, str]:
    """Cancel a running or paused job."""
    controller = _active_jobs.get(job_id)
    if controller is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    controller.request_cancel()
    return {"job_id": job_id, "status": controller.status.value}
