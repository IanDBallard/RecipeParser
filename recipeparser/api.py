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

from recipeparser.models import IngestResponse
from recipeparser.utils import temp_file_from_upload, html_to_text
from recipeparser.pipeline import run_cayenne_pipeline

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
        return {'sub': 'local-e2e-test-user', 'aud': 'authenticated'}

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
        return og_match.group(1).strip().rstrip(')')

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

@app.post('/ingest', response_model=IngestResponse)
async def ingest_recipe(
    request: IngestRequest,
    _user: dict = Depends(_verify_supabase_jwt),
):
    """Ingest a recipe from plain text."""
    source_text = request.text
    if not source_text or not source_text.strip():
        raise HTTPException(
            status_code=400,
            detail='Only text ingestion is supported on this endpoint. For URL ingestion use POST /ingest/url.',
        )

    try:
        client = _get_client()
        return _safe_run_pipeline(
            source_text=source_text,
            client=client,
            uom_system=request.uom_system or 'US',
            measure_preference=request.measure_preference or 'Volume',
            source_url=request.url or None,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/ingest/url', response_model=IngestResponse)
async def ingest_url(
    request: IngestUrlRequest,
    _user: dict = Depends(_verify_supabase_jwt),
):
    """Fetch a recipe page via Jina, extract text, and run the pipeline."""
    import httpx
    from recipeparser.epub import is_recipe_candidate

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

    # Extract hero image URL from Jina's markdown before running the pipeline.
    # Upload is best-effort — failure does not abort ingestion.
    user_id = _user.get('sub', 'unknown')
    recipe_id = str(uuid.uuid4())
    candidate_image_url = _extract_image_url_from_markdown(raw_markdown)
    image_url: Optional[str] = None
    if candidate_image_url:
        image_url = _upload_image_to_storage(candidate_image_url, user_id, recipe_id)

    try:
        client = _get_client()
        return _safe_run_pipeline(
            source_text=source_text,
            client=client,
            uom_system=request.uom_system or 'US',
            measure_preference=request.measure_preference or 'Volume',
            source_url=url,
            image_url=image_url,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/ingest/pdf', response_model=IngestResponse)
async def ingest_pdf(
    file: UploadFile = File(...),
    uom_system: str = Form('US'),
    measure_preference: str = Form('Volume'),
    _user: dict = Depends(_verify_supabase_jwt),
):
    """Ingest a recipe from an uploaded PDF."""
    from recipeparser.pdf import extract_text_from_pdf

    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail='File must be a PDF.')

    try:
        client = _get_client()
        with temp_file_from_upload(file) as tmp_path:
            try:
                source_text = extract_text_from_pdf(tmp_path, client=client)
            except Exception as e:
                raise HTTPException(status_code=422, detail=str(e))

        if not source_text.strip():
            raise HTTPException(status_code=422, detail='No text could be extracted from the PDF.')

        return _safe_run_pipeline(
            source_text=source_text,
            client=client,
            uom_system=uom_system,
            measure_preference=measure_preference,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/ingest/epub', response_model=IngestResponse)
async def ingest_epub(
    file: UploadFile = File(...),
    uom_system: str = Form('US'),
    measure_preference: str = Form('Volume'),
    _user: dict = Depends(_verify_supabase_jwt),
):
    """Ingest a recipe from an uploaded EPUB."""
    from recipeparser.epub import extract_text_from_epub

    if not file.filename.lower().endswith('.epub'):
        raise HTTPException(status_code=400, detail='File must be an EPUB.')

    try:
        client = _get_client()
        with temp_file_from_upload(file) as tmp_path:
            try:
                source_text = extract_text_from_epub(tmp_path)
            except Exception as e:
                raise HTTPException(status_code=422, detail=str(e))

        if not source_text.strip():
            raise HTTPException(status_code=422, detail='No text could be extracted from the EPUB.')

        return _safe_run_pipeline(
            source_text=source_text,
            client=client,
            uom_system=uom_system,
            measure_preference=measure_preference,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


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
