"""FastAPI application for the Cayenne Ingestion API.

Endpoints (canonical — Phase 6):
  POST /jobs              — fire-and-forget URL/text job (returns 202 + { job_id })
  POST /jobs/file         — fire-and-forget file upload job (returns 202 + { job_id })
  GET  /jobs/{job_id}     — poll job status from _active_jobs registry
  POST /jobs/{job_id}/pause   — pause a running job
  POST /jobs/{job_id}/resume  — resume a paused job
  POST /jobs/{job_id}/cancel  — cancel a job
  POST /embed             — generate a 1536-dim embedding (returns 200 + {embedding})
  GET  /health            — liveness probe + the auth mode the app booted with

Auth:
  HTTPBearer JWT verified against Supabase; the decoded ``sub`` claim is the
  user_id every write is attributed to.

  DISABLE_AUTH=1 + TEST_USER_ID=<uuid> bypasses verification. It exists for the
  automated test suite and explicitly opted-in local development only. The
  bypass is deliberately strict — a UUID TEST_USER_ID is mandatory and a
  production/staging APP_ENV refuses it outright — because a bypass that half
  works is worse than one that fails: it silently files every ingested recipe
  under the wrong subject, where PowerSync will never sync it to the user who
  submitted it. GET /health reports which mode is live.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import uuid
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import httpx
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from recipeparser.adapters.job_sink import JobSink
from recipeparser.config import live_writes_blocked as _live_writes_blocked
from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType
from recipeparser.core.pipeline import RecipePipeline
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource
from recipeparser.io.readers.epub import EpubReader as _EpubReader
from recipeparser.io.readers.paprika import PaprikaReader as _PaprikaReader
from recipeparser.io.readers.pdf import PdfReader as _PdfReader
from recipeparser.io.writers.image_store import SupabaseImageStore
from recipeparser.io.writers.supabase import write_recipe_to_supabase
import recipeparser.gemini as _gemini_mod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App + auth setup
# ---------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on"}

# Deployment tiers that must never boot with JWT verification switched off.
_PROTECTED_TIERS = {"production", "prod", "staging", "stage"}


class AuthConfigurationError(RuntimeError):
    """Raised when the auth bypass is requested in an unsafe configuration."""


def _resolve_auth_mode(env: Mapping[str, str]) -> tuple[bool, str]:
    """Resolve the auth mode from the environment.

    Returns ``(bypass_engaged, test_user_id)``.

    The bypass is opt-in and deliberately unforgiving. ``DISABLE_AUTH`` can
    arrive from a shell, a launcher, a CI job or a stray ``.env`` line that
    ``recipeparser/__init__.py`` loads on import, so anything short of a fully
    specified bypass raises instead of quietly serving an unauthenticated API:

      * ``TEST_USER_ID`` must be an explicit UUID. There is no fallback
        identity — a placeholder subject writes rows no real user can ever
        sync.
      * ``APP_ENV`` / ``ENVIRONMENT`` must not name a protected tier.
    """
    if env.get("DISABLE_AUTH", "0").strip().lower() not in _TRUTHY:
        return False, ""

    tier = (env.get("APP_ENV") or env.get("ENVIRONMENT") or "").strip().lower()
    if tier in _PROTECTED_TIERS:
        raise AuthConfigurationError(
            f"DISABLE_AUTH is set but the deployment tier is {tier!r}. The auth "
            "bypass is for automated tests and local development only — unset "
            "DISABLE_AUTH."
        )

    test_user_id = env.get("TEST_USER_ID", "").strip()
    if not test_user_id:
        raise AuthConfigurationError(
            "DISABLE_AUTH is set but TEST_USER_ID is empty. The bypass "
            "attributes every ingested recipe to TEST_USER_ID, so it has to be "
            "stated explicitly rather than defaulted."
        )
    try:
        uuid.UUID(test_user_id)
    except ValueError as exc:
        raise AuthConfigurationError(
            f"TEST_USER_ID={test_user_id!r} is not a UUID. Supabase subjects are "
            "UUIDs; anything else writes rows that no user can sync."
        ) from exc

    return True, test_user_id


# The one place in the codebase allowed to name the old variable: it exists
# only to detect and reject a half-configured deployment, never to read the
# value. tests/unit/test_service_key_name.py's tree-wide scan allowlists this
# module by name for exactly that reason — see the comment there.
_LEGACY_SERVICE_KEY_ENV = "SUPABASE_SERVICE_KEY"


def check_service_key_name() -> None:
    """Refuse to boot half-configured.

    The service key was read under two names in different modules, so setting
    only one disabled a subset of writes with a warning rather than an error —
    the worst failure mode available, because most of the app kept working.
    """
    if os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        return
    if os.environ.get(_LEGACY_SERVICE_KEY_ENV):
        raise RuntimeError(
            f"{_LEGACY_SERVICE_KEY_ENV} is set but SUPABASE_SERVICE_ROLE_KEY is not. "
            "The latter is now the only name read. Rename the variable."
        )


_DISABLE_AUTH, _TEST_USER_ID = _resolve_auth_mode(os.environ)
_bearer = HTTPBearer(auto_error=not _DISABLE_AUTH)

if _DISABLE_AUTH:
    logger.critical(
        "AUTH BYPASS ENGAGED: JWT verification is OFF and every request is "
        "attributed to user %s. Never run this configuration in production.",
        _TEST_USER_ID,
    )


def _worker_enabled(env: Mapping[str, str]) -> bool:
    """REGEN_WORKER_ENABLED gates the background regen/recategorise workers."""
    return env.get("REGEN_WORKER_ENABLED", "").strip().lower() in _TRUTHY


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start the background workers when enabled; stop them cleanly on shutdown."""
    task: Optional[asyncio.Task] = None
    stop = asyncio.Event()
    if _worker_enabled(os.environ):
        supabase = _get_supabase_service_client()
        if supabase is None:
            logger.warning("REGEN_WORKER_ENABLED is set but Supabase is not configured — workers not started.")
        else:
            # Imported here so the worker modules are not a hard dependency of the API import.
            from recipeparser.adapters import regen_worker as _rw  # noqa: PLC0415
            workers: list = [_rw.RegenWorker(supabase, _get_client())]
            try:
                from recipeparser.adapters.recat_worker import RecatWorker  # noqa: PLC0415
                workers.append(RecatWorker(supabase, _get_client()))
            except ImportError:
                pass  # Task 10 adds it; the regen worker runs alone until then.
            task = asyncio.create_task(_rw.run_workers(workers, stop))
            logger.info("Background workers started: %s", [type(w).__name__ for w in workers])
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            await task


app = FastAPI(title="Cayenne Ingestion API", version="1.0.0", lifespan=_lifespan)
check_service_key_name()

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# The Cayenne client is a browser app served from its own origin, so every call
# here is cross-origin and starts with a preflight OPTIONS that FastAPI answers
# 405 to unless this middleware is installed. The client sends an Authorization
# header and a JSON body, so both must be allowed for the real request to follow.
#
# Origins are configured, never wildcarded: `allow_credentials` with "*" is
# rejected by browsers, and an ingestion endpoint that writes to a user's
# library should not answer any page on the internet. Set CORS_ORIGINS to a
# comma-separated list to override the development defaults below (a deployment
# adds its own origin; a tailnet or tunnel host used for phone testing adds
# that host, scheme and port included).
_DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173,"   # vite dev
    "http://localhost:4173,"   # vite preview / sirv on the built app
    "http://localhost:4174"    # playwright's e2e build
)
_cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
logger.info("CORS enabled for: %s", ", ".join(_cors_origins))


@app.get("/health", status_code=200)
def health() -> dict[str, str]:
    """Liveness probe reporting the auth mode the app booted with.

    A bypassed server is indistinguishable from a verifying one until it
    misattributes a write, so the mode is published rather than inferred.
    """
    return {
        "status": "ok",
        "auth_mode": "bypassed" if _DISABLE_AUTH else "verifying",
    }


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _blank_subject(sub: Any) -> bool:
    """True if *sub* is missing, ``None``, empty, or whitespace-only.

    A blank subject must never authenticate, and must never match anything as
    an owner — including another blank subject. `_verify_supabase_jwt` and
    `_owned_controller` both call this so the two stay in agreement.
    """
    return not isinstance(sub, str) or not sub.strip()


def _verify_supabase_jwt(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict[str, Any]:
    """Verify a Supabase-issued JWT and return the decoded payload.

    When the bypass is engaged the token is not verified and the validated
    TEST_USER_ID resolved at import time is used as the subject claim.
    """
    if _DISABLE_AUTH:
        return {"sub": _TEST_USER_ID}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    token = credentials.credentials
    supabase_url = os.environ.get("SUPABASE_URL", "")
    # (the SUPABASE_SERVICE_ROLE_KEY read that was here is gone: JWT verification
    #  uses the JWKS endpoint, and the variable was never referenced)

    try:
        import jwt as pyjwt  # noqa: PLC0415

        jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
        jwks_client = pyjwt.PyJWKClient(jwks_url)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        # Supabase's current signing keys are ECC (ES256); older projects, and any
        # project that chooses RSA, sign with RS256. The JWKS client selects the key
        # by the token's `kid`, so accepting both algorithms verifies either kind and
        # still refuses a token whose algorithm is anything else (`none` included).
        payload: dict[str, Any] = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc

    # Every write in this service is attributed to `sub`. A token that clears
    # signature verification but carries no subject (empty or whitespace-only
    # included) would otherwise create rows owned by that blank value — and
    # since the ownership check in _owned_controller is a plain equality, a
    # second sub-less token would match the same blank value and be handed
    # the first caller's job.
    if _blank_subject(payload.get("sub")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has no subject claim",
        )
    return payload


# ---------------------------------------------------------------------------
# Client factory (mockable)
# ---------------------------------------------------------------------------

def _get_client() -> Any:
    """Create and return a Gemini generative client (google.genai.Client).

    Raises RuntimeError if GOOGLE_API_KEY is not set.
    This function is a named top-level so tests can patch it via
    ``recipeparser.adapters.api._get_client``.
    """
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not found in environment")
    from google import genai  # noqa: PLC0415
    return genai.Client(api_key=api_key)


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
    """Download *image_url* and store it, returning the public URL or None.

    A thin wrapper over the same ImageStore the pipeline uses: this path exists
    for URL submissions, which arrive with an address rather than bytes. Failures
    are logged but never raised — a recipe without a picture, not a failed job.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(image_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            data = response.content
    except Exception:
        logger.exception("Could not fetch %s for recipe %s — continuing without an image.", image_url, recipe_id)
        return None
    return await asyncio.to_thread(SupabaseImageStore().put, data, recipe_id, content_type)


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

def _get_supabase_service_client() -> Any:
    """Return a synchronous supabase-py client using the service role key.

    Returns None if credentials are not configured (test/offline mode), or if this
    is a test run that must not reach a real project (see _live_writes_blocked).
    """
    if _live_writes_blocked():
        logger.warning("Test run: refusing to build a Supabase client against a real project.")
        return None
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not supabase_key:
        return None
    from supabase import create_client  # type: ignore[import-not-found]
    return create_client(supabase_url, supabase_key)


def _resolve_prefs(user_id: str, body_uom: str, body_measure: str) -> Tuple[str, str]:
    """Unit preferences for a job: the profiles row wins, the request body is the fallback.

    Same query as regen_worker.load_profile_prefs, but the fallback differs: the
    worker has no request body, so it defaults to US / Volume; ingestion has one.
    """
    supabase = _get_supabase_service_client()
    if supabase is None:
        return body_uom, body_measure
    try:
        res = (
            supabase.table("profiles")
            .select("uom_system,measure_preference")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("profiles lookup failed for %s (%s) — using request values.", user_id, exc)
        return body_uom, body_measure
    rows = res.data or []
    if not rows:
        return body_uom, body_measure
    row = rows[0]
    return (row.get("uom_system") or body_uom, row.get("measure_preference") or body_measure)


def _create_ingestion_job(
    job_id: str,
    user_id: str,
    source_hint: Optional[str] = None,
) -> None:
    """INSERT the initial ingestion_jobs row into Supabase.

    Called synchronously before the background task is fired so the row
    exists immediately and the Cayenne app can start polling.

    Failures are logged but NOT re-raised — a missing job row is bad UX
    but must never prevent the job from starting.
    """
    import datetime
    sb = _get_supabase_service_client()
    if sb is None:
        logger.warning("Job %s: Supabase credentials not set — skipping ingestion_jobs INSERT.", job_id)
        return
    now = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        sb.table("ingestion_jobs").insert({
            "id": job_id,
            "user_id": user_id,
            "status": "running",
            "stage": "IDLE",
            "progress_pct": 0,
            "recipe_count": 0,
            "skipped_count": 0,
            "skipped": [],
            "source_hint": source_hint,
            "error_message": None,
            "created_at": now,
            "updated_at": now,
        }).execute()
        logger.info("Job %s: ingestion_jobs row created (user=%s).", job_id, user_id)
    except Exception:
        # Not raised — a raise here would break the endpoint contract — but this
        # is not a recoverable condition: the client only ever polls this row, so
        # with no row it will show nothing for this job, forever. The first thing
        # to check is a schema mismatch: this INSERT (and JobSink.finalize_payload)
        # write `skipped_count` / `skipped`, columns added by Cayenne migration 009,
        # and PostgREST rejects an INSERT naming a column that does not exist yet.
        logger.error(
            "Job %s: COULD NOT CREATE ingestion_jobs ROW — the client will never "
            "see this job. First suspect: a schema mismatch, e.g. missing "
            "skipped_count/skipped columns (Cayenne migration 009 not applied).",
            job_id,
            exc_info=True,
        )


def _finalize_ingestion_job(job_id: str, payload: dict[str, Any]) -> None:
    """UPDATE ingestion_jobs to the terminal state the JobSink built.

    The payload comes from JobSink.finalize_payload, which is where the rules
    about counts and progress live.  Failures are logged but NOT re-raised.
    """
    if _live_writes_blocked():
        logger.warning("Test run: skipping the finalize for job %s.", job_id)
        return
    sb = _get_supabase_service_client()
    if sb is None:
        logger.warning("Job %s: Supabase credentials not set — skipping ingestion_jobs finalize.", job_id)
        return
    try:
        sb.table("ingestion_jobs").update(payload).eq("id", job_id).execute()
        logger.info(
            "Job %s: finalized — status=%s, recipes=%s, skipped=%s.",
            job_id, payload.get("status"), payload.get("recipe_count"), payload.get("skipped_count"),
        )
    except Exception:
        logger.exception("Job %s: failed to finalize ingestion_jobs row.", job_id)


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
        if _live_writes_blocked():
            logger.warning("Test run: skipping the stage update for job %s.", job_id)
            return
        if not supabase_url or not supabase_key:
            logger.warning(
                "Job %s: Supabase credentials not set — cannot update stage to '%s'.",
                job_id, stage,
            )
            return
        try:
            import datetime
            from supabase import create_client  # type: ignore[import-not-found]
            sb = create_client(supabase_url, supabase_key)
            sb.table("ingestion_jobs").update({
                "stage": stage,
                "status": "running",
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            }).eq("id", job_id).execute()
            logger.debug("Job %s: stage → %s", job_id, stage)
        except Exception:
            logger.exception(
                "Job %s: failed to update ingestion_jobs.stage to '%s' — re-raising (§11.4).",
                job_id, stage,
            )
            raise

    return _on_stage_change


def _make_progress_writer(job_id: str) -> Callable[[int], None]:
    """Write one whole-percent progress update to the job row.

    Unlike the stage callback this does not re-raise: a missed percentage is a
    bar that lags, not a job whose state is unknowable. Because of that, the
    *entire* body — including building the Supabase client — runs inside the
    try/except: RecipePipeline.run's on_progress callback deliberately
    re-raises, so any exception that escaped this function would kill the
    whole import over nothing worse than a stale progress bar.
    """
    def _write(pct: int) -> None:
        if _live_writes_blocked():
            return
        try:
            import datetime  # noqa: PLC0415

            sb = _get_supabase_service_client()
            if sb is None:
                return
            sb.table("ingestion_jobs").update({
                "progress_pct": pct,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
            }).eq("id", job_id).execute()
        except Exception:
            logger.exception("Job %s: failed to write progress %d%%.", job_id, pct)

    return _write


def _update_total_chunks(job_id: str, total: int) -> None:
    """Record how many chunks this job will attempt.

    The ingestion_jobs row is inserted before the background task is scheduled
    and the source is only read inside _run(), so the INSERT cannot carry this
    number — it arrives as an UPDATE once the reader has returned and before
    the pipeline starts. The column is nullable for exactly that window.

    Like the progress writer and unlike everything else here, this logs and
    returns rather than re-raising (§11.4's third deliberate exception): a
    missing denominator is a notice that omits "of 827", not a job whose state
    is unknowable. The entire body — including building the client, which can
    itself raise — is inside the try, which is also what keeps a deploy that
    ran before Cayenne migration 011 costing one number instead of the import.
    """
    if _live_writes_blocked():
        logger.warning("Test run: skipping the total_chunks update for job %s.", job_id)
        return
    try:
        import datetime  # noqa: PLC0415

        sb = _get_supabase_service_client()
        if sb is None:
            return
        sb.table("ingestion_jobs").update({
            "total_chunks": total,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        }).eq("id", job_id).execute()
        logger.info("Job %s: total_chunks = %d.", job_id, total)
    except Exception:
        logger.exception("Job %s: failed to write total_chunks=%d.", job_id, total)


# Process-level registry: job_id → (user_id, PipelineController)
# The user id is kept so the control endpoints can refuse a job the caller does
# not own; without it any caller who reached the API could cancel any import.
_active_jobs: Dict[str, tuple[str, PipelineController]] = {}


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
    # .paprikarecipes files are ZIP archives; browsers/Node may send them as
    # application/zip or application/octet-stream — match by extension first,
    # then fall back to content-type + filename suffix check.
    if ext == ".paprikarecipes":
        return "paprika"
    if content_type in ("application/zip", "application/octet-stream") and filename.lower().endswith(".paprikarecipes"):
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
    _active_jobs[job_id] = (user_id, controller)

    async def _run() -> None:
        sink = None
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
            # A list of one, so the total_chunks write below and the run() call
            # take the same shape here as they do in the file endpoint.
            chunks = [
                Chunk(
                    text=source_text,
                    input_type=InputType.URL,
                    source_url=source_url,
                    image_url=stored_image_url,
                )
            ]

            category_source = SupabaseCategorySource()
            category_ids = category_source.load_category_ids(user_id)
            sink = JobSink(
                job_id=job_id,
                user_id=user_id,
                category_ids=category_ids,
                write=write_recipe_to_supabase,
            )
            write_progress = _make_progress_writer(job_id)

            def _on_progress(stage: str, completed: int, total: int) -> None:
                before = len(sink.progress_updates)
                sink.on_progress(stage, completed, total)
                if len(sink.progress_updates) > before:
                    write_progress(sink.progress_updates[-1])

            uom_system, measure_preference = await asyncio.to_thread(
                _resolve_prefs, user_id, body.uom_system, body.measure_preference
            )
            pipeline = RecipePipeline(
                client=client,
                controller=controller,
                category_source=category_source,
                uom_system=uom_system,
                measure_preference=measure_preference,
                image_store=SupabaseImageStore(),
            )
            await asyncio.to_thread(_update_total_chunks, job_id, len(chunks))
            await asyncio.to_thread(
                lambda: pipeline.run(
                    chunks,
                    on_progress=_on_progress,
                    user_id=user_id,
                    on_result=sink.on_result,
                    on_skip=sink.on_skip,
                )
            )
            logger.info(
                "Job %s completed — %d recipe(s), %d skipped.",
                job_id, sink.recipe_count, sink.skipped_count,
            )
            await asyncio.to_thread(
                _finalize_ingestion_job,
                job_id,
                # The controller is back at IDLE by now, so the flag is the only
                # thing that still knows the user pressed Cancel (spec 5.1).
                sink.finalize_payload(True, cancelled=controller.cancel_requested),
            )
        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
            controller.transition("error")
            payload = (
                sink.finalize_payload(False, str(exc))
                if sink is not None
                else {"status": "error", "stage": "ERROR", "error_message": str(exc)}
            )
            await asyncio.to_thread(_finalize_ingestion_job, job_id, payload)
        finally:
            _active_jobs.pop(job_id, None)

    _create_ingestion_job(job_id, user_id, source_hint=body.url or None)
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    file_bytes = await file.read()
    user_id: str = user.get("sub", "")
    job_id = str(uuid.uuid4())
    controller = PipelineController(on_stage_change=_make_stage_callback(job_id))
    _active_jobs[job_id] = (user_id, controller)

    async def _run() -> None:
        # NOTE: Do NOT call controller.transition("start") here.
        # RecipePipeline.run() calls it internally (IDLE → RUNNING).
        # Calling it here first would cause an invalid double-transition.
        sink = None
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

            category_source = SupabaseCategorySource()
            category_ids = category_source.load_category_ids(user_id)
            sink = JobSink(
                job_id=job_id,
                user_id=user_id,
                category_ids=category_ids,
                write=write_recipe_to_supabase,
            )
            write_progress = _make_progress_writer(job_id)

            def _on_progress(stage: str, completed: int, total: int) -> None:
                before = len(sink.progress_updates)
                sink.on_progress(stage, completed, total)
                if len(sink.progress_updates) > before:
                    write_progress(sink.progress_updates[-1])

            # RecipePipeline.run() transitions IDLE→RUNNING internally,
            # processes all chunks (with per-chunk error isolation), then
            # transitions RUNNING→IDLE on success.
            uom_system_resolved, measure_preference_resolved = await asyncio.to_thread(
                _resolve_prefs, user_id, uom_system, measure_preference
            )
            pipeline = RecipePipeline(
                client=client,
                controller=controller,
                category_source=category_source,
                uom_system=uom_system_resolved,
                measure_preference=measure_preference_resolved,
                image_store=SupabaseImageStore(),
            )
            await asyncio.to_thread(_update_total_chunks, job_id, len(chunks))
            await asyncio.to_thread(
                lambda: pipeline.run(
                    chunks,
                    on_progress=_on_progress,
                    user_id=user_id,
                    on_result=sink.on_result,
                    on_skip=sink.on_skip,
                )
            )
            logger.info(
                "Job %s completed — %d recipe(s), %d skipped.",
                job_id, sink.recipe_count, sink.skipped_count,
            )
            await asyncio.to_thread(
                _finalize_ingestion_job,
                job_id,
                # The controller is back at IDLE by now, so the flag is the only
                # thing that still knows the user pressed Cancel (spec 5.1).
                sink.finalize_payload(True, cancelled=controller.cancel_requested),
            )
        except Exception as exc:
            logger.error("File job %s failed: %s", job_id, exc, exc_info=True)
            # Transition to IDLE via "error" event.  The FSM allows this from
            # RUNNING, PAUSING, and RESUMING states.  If the pipeline never
            # started (e.g. reader raised before pipeline.run()), the controller
            # is still IDLE and the transition is a no-op (logs a warning).
            controller.transition("error")
            payload = (
                sink.finalize_payload(False, str(exc))
                if sink is not None
                else {"status": "error", "stage": "ERROR", "error_message": str(exc)}
            )
            await asyncio.to_thread(_finalize_ingestion_job, job_id, payload)
        finally:
            _active_jobs.pop(job_id, None)

    _create_ingestion_job(job_id, user_id, source_hint=filename or None)
    asyncio.create_task(_run())
    return AsyncJobResponse(job_id=job_id)


def _owned_controller(job_id: str, user: dict[str, Any]) -> PipelineController:
    """Return the caller's own job, or 404.

    Not 403 for someone else's job: that would confirm the id exists and make
    the registry enumerable.

    A blank caller subject is refused outright, independently of whether it
    matches the stored owner: _verify_supabase_jwt already rejects a blank
    subject before a real request can reach here, but this is a second line
    of defence — a blank subject must never match anything, including a
    registry entry that (via some future bug, or a pre-fix leftover row) is
    itself owned by a blank value.
    """
    sub = user.get("sub", "")
    entry = _active_jobs.get(job_id)
    if _blank_subject(sub) or entry is None or entry[0] != sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    return entry[1]


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}  — status polling
# ---------------------------------------------------------------------------

@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(
    job_id: str,
    user: dict[str, Any] = Depends(_verify_supabase_jwt),
) -> JobStatusResponse:
    """Return the current FSM status of the caller's running job.

    Returns 404 if the job is not in the active registry (already completed
    or never existed) or is not owned by the caller.
    """
    controller = _owned_controller(job_id, user)
    return JobStatusResponse(job_id=job_id, status=controller.status.value)


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/pause|resume|cancel  — control endpoints
# ---------------------------------------------------------------------------

@app.post("/jobs/{job_id}/pause", status_code=200)
def pause_job(job_id: str, user: dict[str, Any] = Depends(_verify_supabase_jwt)) -> dict[str, str]:
    """Request a pause on the caller's running job."""
    controller = _owned_controller(job_id, user)
    controller.request_pause()
    return {"job_id": job_id, "status": controller.status.value}


@app.post("/jobs/{job_id}/resume", status_code=200)
def resume_job(job_id: str, user: dict[str, Any] = Depends(_verify_supabase_jwt)) -> dict[str, str]:
    """Resume the caller's paused job."""
    controller = _owned_controller(job_id, user)
    controller.request_resume()
    return {"job_id": job_id, "status": controller.status.value}


@app.post("/jobs/{job_id}/cancel", status_code=200)
def cancel_job(job_id: str, user: dict[str, Any] = Depends(_verify_supabase_jwt)) -> dict[str, str]:
    """Cancel the caller's running or paused job."""
    controller = _owned_controller(job_id, user)
    controller.request_cancel()
    return {"job_id": job_id, "status": controller.status.value}
