from fastapi import FastAPI, HTTPException, Depends, Security, UploadFile, File, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Optional, List
import logging
import os
import re
import uuid
import jwt  # PyJWT
from dotenv import load_dotenv

from recipeparser.models import IngestResponse, JobResponse
from recipeparser.utils import temp_file_from_upload, html_to_text
from recipeparser.core.engine import run_cayenne_pipeline
from recipeparser.io.writers.supabase import write_recipe_to_supabase
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource

log = logging.getLogger(__name__)

load_dotenv()

app = FastAPI(title='Cayenne Ingestion API')

# ---------------------------------------------------------------------------
# Auth — Supabase JWT verification
# ---------------------------------------------------------------------------
_DISABLE_AUTH = os.getenv('DISABLE_AUTH', '0') == '1'
_bearer = HTTPBearer(auto_error=not _DISABLE_AUTH)


def _verify_supabase_jwt(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> dict:
    if _DISABLE_AUTH:
        test_user_id = os.getenv('TEST_USER_ID')
        if not test_user_id:
            raise HTTPException(
                status_code=500,
                detail='DISABLE_AUTH=1 requires TEST_USER_ID env var to be set.',
            )
        return {'sub': test_user_id, 'aud': 'authenticated'}

    if credentials is None:
        raise HTTPException(status_code=403, detail='Not authenticated.')

    jwt_secret = os.getenv('SUPABASE_JWT_SECRET')
    if not jwt_secret:
        raise HTTPException(
            status_code=500,
            detail='Server misconfiguration: SUPABASE_JWT_SECRET not set.',
        )

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            jwt_secret,
            algorithms=['HS256'],
            audience='authenticated',
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail='Token has expired.')
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f'Invalid token: {exc}')


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    text: Optional[str] = None
    url: Optional[str] = None
    uom_system: Optional[str] = 'US'
    measure_preference: Optional[str] = 'Volume'


class IngestUrlRequest(BaseModel):
    url: str
    uom_system: Optional[str] = 'US'
    measure_preference: Optional[str] = 'Volume'


class EmbedRequest(BaseModel):
    text: str = Field(..., description="The query string to vectorize for semantic search.")


class EmbedResponse(BaseModel):
    embedding: List[float]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_client():
    """Lazily initialise the Gemini client."""
    from google import genai
    api_key = os.getenv('GOOGLE_API_KEY')
    if not api_key:
        raise RuntimeError('GOOGLE_API_KEY not found in environment.')
    return genai.Client(api_key=api_key)


def _safe_run_pipeline(source_text: str, client, **kwargs) -> IngestResponse:
    """Wrapper to catch pipeline errors and map to FastAPI exceptions."""
    try:
        return run_cayenne_pipeline(source_text, client, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected pipeline error: {e}")


def _fetch_user_axes_and_ids(user_id: str) -> tuple[dict, dict]:
    """
    Fetch the user's multipolar taxonomy axes and category UUID map from Supabase.

    Returns (user_axes, category_ids) — both are empty dicts if the user has
    no categories defined or if Supabase credentials are not configured.
    This is non-fatal: the pipeline will simply produce no categories
    (Zero-Tag Mandate).
    """
    source = SupabaseCategorySource()
    user_axes = source.load_axes(user_id)
    category_ids = source.load_category_ids(user_id) if user_axes else {}
    return user_axes, category_ids


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

# Matches the first Markdown image tag: ![alt](url) — used to find og:image
# equivalents in Jina's markdown output.
_MD_IMAGE_RE = re.compile(r'!\[[^\]]*\]\((https?://[^)]+)\)')

# Matches an explicit og:image or twitter:image meta line that Jina sometimes
# surfaces as plain text in its markdown output.
_OG_IMAGE_RE = re.compile(
    r'(?:og:image|twitter:image)["\s:]+\s*(https?://\S+)', re.IGNORECASE
)


def _extract_image_url_from_markdown(markdown: str) -> Optional[str]:
    """
    Attempt to find a hero image URL from Jina's markdown output.

    Priority:
    1. og:image / twitter:image meta line (most reliable)
    2. First Markdown image tag in the document

    Returns the URL string, or None if no image is found.
    """
    og_match = _OG_IMAGE_RE.search(markdown)
    if og_match:
        url = og_match.group(1).strip()
        # Remove at most one trailing ')' when Jina markdown has an extra one
        # (e.g. meta was in parens). Do not use rstrip(')') — that would corrupt
        # legitimate URLs containing parentheses, e.g. Wikipedia .../path(name).
        if url.endswith('))'):
            url = url[:-1]
        return url

    md_match = _MD_IMAGE_RE.search(markdown)
    if md_match:
        return md_match.group(1).strip()

    return None


def _upload_image_to_storage(
    image_url: str,
    user_id: str,
    recipe_id: str,
) -> Optional[str]:
    """
    Download an image from ``image_url`` and upload it to the Supabase
    ``recipe-images`` bucket.

    Returns the public Supabase Storage URL, or None if the upload fails
    (non-fatal — the recipe is still saved without a photo).

    Requires env vars:
      SUPABASE_URL          — e.g. https://<ref>.supabase.co
      SUPABASE_SERVICE_KEY  — service-role key (never the anon key)
    """
    import httpx

    supabase_url = os.getenv('SUPABASE_URL', '').rstrip('/')
    service_key = os.getenv('SUPABASE_SERVICE_KEY', '')

    if not supabase_url or not service_key:
        log.warning(
            'Image upload skipped: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.'
        )
        return None

    # --- Download the source image ---
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as http:
            resp = http.get(image_url)
            resp.raise_for_status()
            image_bytes = resp.content
            content_type = resp.headers.get('content-type', 'image/jpeg').split(';')[0].strip()
    except Exception as exc:
        log.warning('Image download failed (%s): %s', image_url, exc)
        return None

    # Derive a file extension from content-type (default .jpg)
    _EXT_MAP = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/gif': '.gif',
    }
    ext = _EXT_MAP.get(content_type, '.jpg')
    storage_path = f'{user_id}/{recipe_id}{ext}'

    # --- Upload to Supabase Storage ---
    upload_url = f'{supabase_url}/storage/v1/object/recipe-images/{storage_path}'
    headers = {
        'Authorization': f'Bearer {service_key}',
        'Content-Type': content_type,
        'x-upsert': 'true',  # overwrite if re-ingesting the same recipe
    }
    try:
        with httpx.Client(timeout=30.0) as http:
            up_resp = http.post(upload_url, content=image_bytes, headers=headers)
            up_resp.raise_for_status()
    except Exception as exc:
        log.warning('Image upload to Supabase Storage failed: %s', exc)
        return None

    public_url = (
        f'{supabase_url}/storage/v1/object/public/recipe-images/{storage_path}'
    )
    log.info('Image uploaded → %s', public_url)
    return public_url


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

async def _ingest_text_handler(
    request: IngestRequest,
    _user: dict,
) -> JobResponse:
    """
    Shared implementation for POST /ingest/text (and its legacy alias POST /ingest).

    Ingests a recipe from plain text. The pipeline runs extract → refine → embed
    via Gemini, then writes the result directly to Supabase.

    ARCHITECTURAL INVARIANT: The API writes the recipe directly to Supabase and
    returns only a lightweight { job_id, recipe_id } acknowledgment (202).
    The client app NEVER receives recipe JSON. Recipes reach the client via PowerSync.
    """
    source_text = request.text
    if not source_text or not source_text.strip():
        raise HTTPException(
            status_code=400,
            detail='text field is required and must not be empty. For URL ingestion use POST /ingest/url.',
        )

    user_id = _user.get('sub', 'unknown')
    job_id = str(uuid.uuid4())

    try:
        client = _get_client()
        user_axes, category_ids = _fetch_user_axes_and_ids(user_id)
        result = _safe_run_pipeline(
            source_text=source_text,
            client=client,
            uom_system=request.uom_system or 'US',
            measure_preference=request.measure_preference or 'Volume',
            source_url=request.url or None,
            user_axes=user_axes,
        )
        recipe_id = write_recipe_to_supabase(
            result, user_id=user_id, category_ids=category_ids
        )
        return JobResponse(job_id=job_id, recipe_id=recipe_id)
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/ingest/text', response_model=JobResponse, status_code=202)
async def ingest_text(
    request: IngestRequest,
    _user: dict = Depends(_verify_supabase_jwt),
):
    """
    Ingest a recipe from plain text.

    Follows the same ``/ingest/<media-type>`` naming convention as the other
    ingestion endpoints (``/ingest/url``, ``/ingest/pdf``, ``/ingest/epub``,
    ``/ingest/paprika``).

    ARCHITECTURAL INVARIANT: The API writes the recipe directly to Supabase and
    returns only a lightweight { job_id, recipe_id } acknowledgment (202).
    The client app NEVER receives recipe JSON. Recipes reach the client via PowerSync.
    """
    return await _ingest_text_handler(request, _user)


@app.post('/ingest', response_model=JobResponse, status_code=202)
async def ingest_recipe(
    request: IngestRequest,
    _user: dict = Depends(_verify_supabase_jwt),
):
    """
    Legacy alias for POST /ingest/text — kept for backward compatibility.

    Prefer POST /ingest/text for new integrations.
    """
    return await _ingest_text_handler(request, _user)


@app.post('/ingest/url', response_model=JobResponse, status_code=202)
async def ingest_url(
    request: IngestUrlRequest,
    _user: dict = Depends(_verify_supabase_jwt),
):
    """
    Fetch a recipe page via Jina, extract text, and run the pipeline.

    ARCHITECTURAL INVARIANT: The API writes the recipe directly to Supabase and
    returns only a lightweight { job_id, recipe_id } acknowledgment (202).
    The client app NEVER receives recipe JSON. Recipes reach the client via PowerSync.
    """
    import httpx
    from recipeparser.io.readers.epub import is_recipe_candidate

    url = (request.url or '').strip()
    if not url:
        raise HTTPException(status_code=400, detail='url field is required.')

    jina_url = f'https://r.jina.ai/{url}'
    headers = {'Accept': 'text/html'}
    jina_api_key = os.getenv('JINA_API_KEY')
    if jina_api_key:
        headers['Authorization'] = f'Bearer {jina_api_key}'

    try:
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            response = await http_client.get(jina_url, headers=headers, follow_redirects=True)
            response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f'Failed to fetch URL: {exc}')

    raw_markdown = response.text
    source_text = html_to_text(raw_markdown)
    if not source_text.strip():
        raise HTTPException(status_code=422, detail='No text could be extracted from the URL.')

    if not is_recipe_candidate(source_text):
        raise HTTPException(status_code=422, detail='URL does not appear to contain a recipe.')

    # Pre-generate the recipe_id so the image can be stored under the correct path
    # before the pipeline runs. The same ID is used for the Supabase row.
    user_id = _user.get('sub', 'unknown')
    job_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())

    candidate_image_url = _extract_image_url_from_markdown(raw_markdown)
    image_url: Optional[str] = None
    if candidate_image_url:
        image_url = _upload_image_to_storage(candidate_image_url, user_id, recipe_id)

    try:
        client = _get_client()
        user_axes, category_ids = _fetch_user_axes_and_ids(user_id)
        result = _safe_run_pipeline(
            source_text=source_text,
            client=client,
            uom_system=request.uom_system or 'US',
            measure_preference=request.measure_preference or 'Volume',
            source_url=url,
            image_url=image_url,
            user_axes=user_axes,
        )
        write_recipe_to_supabase(
            result, user_id=user_id, recipe_id=recipe_id, category_ids=category_ids
        )
        return JobResponse(job_id=job_id, recipe_id=recipe_id)
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/ingest/pdf', response_model=JobResponse, status_code=202)
async def ingest_pdf(
    file: UploadFile = File(...),
    uom_system: str = Form('US'),
    measure_preference: str = Form('Volume'),
    _user: dict = Depends(_verify_supabase_jwt),
):
    """
    Ingest a recipe from an uploaded PDF.

    ARCHITECTURAL INVARIANT: The API writes the recipe directly to Supabase and
    returns only a lightweight { job_id, recipe_id } acknowledgment (202).
    The client app NEVER receives recipe JSON. Recipes reach the client via PowerSync.
    """
    from recipeparser.io.readers.pdf import extract_text_from_pdf

    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail='File must be a PDF.')

    user_id = _user.get('sub', 'unknown')
    job_id = str(uuid.uuid4())

    try:
        client = _get_client()
        with temp_file_from_upload(file) as tmp_path:
            try:
                source_text = extract_text_from_pdf(tmp_path, client=client)
            except Exception as e:
                raise HTTPException(status_code=422, detail=str(e))

        if not source_text.strip():
            raise HTTPException(status_code=422, detail='No text could be extracted from the PDF.')

        user_axes, category_ids = _fetch_user_axes_and_ids(user_id)
        result = _safe_run_pipeline(
            source_text=source_text,
            client=client,
            uom_system=uom_system,
            measure_preference=measure_preference,
            user_axes=user_axes,
        )
        recipe_id = write_recipe_to_supabase(
            result, user_id=user_id, category_ids=category_ids
        )
        return JobResponse(job_id=job_id, recipe_id=recipe_id)
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/ingest/epub', response_model=JobResponse, status_code=202)
async def ingest_epub(
    file: UploadFile = File(...),
    uom_system: str = Form('US'),
    measure_preference: str = Form('Volume'),
    _user: dict = Depends(_verify_supabase_jwt),
):
    """
    Ingest a recipe from an uploaded EPUB.

    ARCHITECTURAL INVARIANT: The API writes the recipe directly to Supabase and
    returns only a lightweight { job_id, recipe_id } acknowledgment (202).
    The client app NEVER receives recipe JSON. Recipes reach the client via PowerSync.
    """
    from recipeparser.io.readers.epub import extract_text_from_epub

    if not file.filename.lower().endswith('.epub'):
        raise HTTPException(status_code=400, detail='File must be an EPUB.')

    user_id = _user.get('sub', 'unknown')
    job_id = str(uuid.uuid4())

    try:
        client = _get_client()
        with temp_file_from_upload(file) as tmp_path:
            try:
                source_text = extract_text_from_epub(tmp_path)
            except Exception as e:
                raise HTTPException(status_code=422, detail=str(e))

        if not source_text.strip():
            raise HTTPException(status_code=422, detail='No text could be extracted from the EPUB.')

        user_axes, category_ids = _fetch_user_axes_and_ids(user_id)
        result = _safe_run_pipeline(
            source_text=source_text,
            client=client,
            uom_system=uom_system,
            measure_preference=measure_preference,
            user_axes=user_axes,
        )
        recipe_id = write_recipe_to_supabase(
            result, user_id=user_id, category_ids=category_ids
        )
        return JobResponse(job_id=job_id, recipe_id=recipe_id)
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


class PaprikaIngestResponse(BaseModel):
    """Summary of a .paprikarecipes batch import."""
    job_ids: List[str]
    recipe_ids: List[str]
    success_count: int
    failure_count: int
    errors: List[str]


@app.post('/ingest/paprika', response_model=PaprikaIngestResponse, status_code=202)
async def ingest_paprika(
    file: UploadFile = File(...),
    uom_system: str = Form('US'),
    measure_preference: str = Form('Volume'),
    _user: dict = Depends(_verify_supabase_jwt),
):
    """
    Ingest a .paprikarecipes archive (Paprika 3 export format).

    Handles both Cayenne-flavored archives (containing ``_cayenne_meta``) and
    legacy Paprika archives in a single endpoint:

    - **Flow B — Cayenne Instant Restore**: If a recipe entry contains
      ``_cayenne_meta``, the pre-structured ``CayenneRecipe`` JSON and
      1536-dim embedding are extracted directly and written to Supabase.
      Gemini is NOT called — zero AI cost.

    - **Flow A — Legacy Paprika**: If ``_cayenne_meta`` is absent, the recipe
      fields are flattened to plain text and run through the full Cayenne
      pipeline (extract → refine → embed via Gemini).

    ARCHITECTURAL INVARIANT: The API is the sole writer to Supabase.
    The client app NEVER writes ingested recipes directly. Recipes reach
    the client via PowerSync sync.

    Returns a summary of all recipes processed (success + failure counts).
    """
    import json as _json
    from recipeparser.io.readers.paprika import PaprikaReader

    if not (file.filename or '').lower().endswith('.paprikarecipes'):
        raise HTTPException(
            status_code=400,
            detail='File must be a .paprikarecipes archive.',
        )

    user_id = _user.get('sub', 'unknown')

    try:
        client = _get_client()
        user_axes, category_ids = _fetch_user_axes_and_ids(user_id)

        with temp_file_from_upload(file) as tmp_path:
            reader = PaprikaReader()
            try:
                entries = reader.read_entries(tmp_path)
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f'Failed to parse .paprikarecipes archive: {exc}',
                )

        if not entries:
            raise HTTPException(
                status_code=422,
                detail='Archive is empty or contains no .paprikarecipe entries.',
            )

        job_ids: List[str] = []
        recipe_ids: List[str] = []
        errors: List[str] = []

        for entry in entries:
            recipe_name = entry.get('name') or 'Untitled'
            job_id = str(uuid.uuid4())

            try:
                cayenne_meta = entry.get('_cayenne_meta')

                if cayenne_meta:
                    # ── Flow B: Cayenne Instant Restore ──────────────────────
                    # The _cayenne_meta blob contains the full CayenneRecipe JSON
                    # plus the 1536-dim embedding. Bypass Gemini entirely.
                    if isinstance(cayenne_meta, str):
                        cayenne_meta = _json.loads(cayenne_meta)

                    embedding: List[float] = cayenne_meta.get('embedding', [])
                    if not isinstance(embedding, list) or len(embedding) != 1536:
                        raise ValueError(
                            f'_cayenne_meta.embedding is invalid '
                            f'(got {type(embedding).__name__} len={len(embedding) if isinstance(embedding, list) else "n/a"}). '
                            f'Falling back to Flow A.'
                        )

                    # Build an IngestResponse from the pre-structured data so
                    # write_recipe_to_supabase() can accept it unchanged.
                    from recipeparser.models import (
                        CayenneRecipe as _CayenneRecipe,
                        StructuredIngredient as _SI,
                        TokenizedDirection as _TD,
                    )
                    structured_ingredients = [
                        _SI(**ing) for ing in cayenne_meta.get('structured_ingredients', [])
                    ]
                    tokenized_directions = [
                        _TD(**d) for d in cayenne_meta.get('tokenized_directions', [])
                    ]
                    restore_recipe = IngestResponse(
                        title=entry.get('name') or cayenne_meta.get('title', 'Untitled'),
                        prep_time=entry.get('prep_time') or cayenne_meta.get('prep_time'),
                        cook_time=entry.get('cook_time') or cayenne_meta.get('cook_time'),
                        base_servings=float(entry.get('servings') or cayenne_meta.get('base_servings') or 0) or None,
                        source_url=entry.get('source_url') or cayenne_meta.get('source_url'),
                        image_url=cayenne_meta.get('image_url'),
                        categories=cayenne_meta.get('categories', []),
                        structured_ingredients=structured_ingredients,
                        tokenized_directions=tokenized_directions,
                        embedding=embedding,
                    )
                    recipe_id = write_recipe_to_supabase(
                        restore_recipe,
                        user_id=user_id,
                        category_ids=category_ids,
                    )
                    log.info(
                        'Flow B (Instant Restore): recipe "%s" written to Supabase id=%s',
                        recipe_name, recipe_id,
                    )

                else:
                    # ── Flow A: Legacy Paprika — full Gemini pipeline ─────────
                    parts: List[str] = []
                    if entry.get('name'):        parts.append(f"Recipe: {entry['name']}")
                    if entry.get('prep_time'):   parts.append(f"Prep Time: {entry['prep_time']}")
                    if entry.get('cook_time'):   parts.append(f"Cook Time: {entry['cook_time']}")
                    if entry.get('servings'):    parts.append(f"Servings: {entry['servings']}")
                    if entry.get('description'): parts.append(f"\nDescription:\n{entry['description']}")
                    if entry.get('ingredients'): parts.append(f"\nIngredients:\n{entry['ingredients']}")
                    if entry.get('directions'):  parts.append(f"\nDirections:\n{entry['directions']}")
                    if entry.get('notes'):       parts.append(f"\nNotes:\n{entry['notes']}")
                    if entry.get('source_url'):  parts.append(f"\nSource: {entry['source_url']}")
                    plain_text = '\n'.join(parts)

                    if not plain_text.strip():
                        raise ValueError(f'Recipe "{recipe_name}" has no extractable text.')

                    result = _safe_run_pipeline(
                        source_text=plain_text,
                        client=client,
                        uom_system=uom_system,
                        measure_preference=measure_preference,
                        source_url=entry.get('source_url'),
                        user_axes=user_axes,
                    )
                    recipe_id = write_recipe_to_supabase(
                        result, user_id=user_id, category_ids=category_ids
                    )
                    log.info(
                        'Flow A (Legacy Paprika): recipe "%s" written to Supabase id=%s',
                        recipe_name, recipe_id,
                    )

                job_ids.append(job_id)
                recipe_ids.append(recipe_id)

            except Exception as exc:
                err_msg = f'"{recipe_name}": {exc}'
                log.warning('paprika ingest error — %s', err_msg)
                errors.append(err_msg)

        return PaprikaIngestResponse(
            job_ids=job_ids,
            recipe_ids=recipe_ids,
            success_count=len(recipe_ids),
            failure_count=len(errors),
            errors=errors,
        )

    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post('/embed', response_model=EmbedResponse)
async def embed_query(
    request: EmbedRequest,
    _user: dict = Depends(_verify_supabase_jwt),
):
    """Stand-alone endpoint to vectorize a search query."""
    from recipeparser.gemini import get_embeddings
    try:
        client = _get_client()
        embedding = get_embeddings(request.text, client)
        return EmbedResponse(embedding=embedding)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8000)
