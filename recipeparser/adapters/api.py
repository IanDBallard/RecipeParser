from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends, Security, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import logging
import os
import re
import uuid
import jwt  # PyJWT
import json
import traceback
import shutil
import tempfile
from pathlib import Path
from dotenv import load_dotenv

# Import utilities and models from the package
from recipeparser.models import CayenneRecipe, IngestResponse, JobResponse
from recipeparser.utils import temp_file_from_upload, html_to_text
from recipeparser.core.engine import run_cayenne_pipeline
from recipeparser.io.writers.supabase import write_recipe_to_supabase
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

load_dotenv()

app = FastAPI(title='Cayenne Ingestion API')

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    log.info(f"Incoming request: {request.method} {request.url}")
    try:
        response = await call_next(request)
        log.info(f"Response status: {response.status_code}")
        return response
    except Exception as e:
        log.error(f"Request failed: {e}")
        log.error(traceback.format_exc())
        raise

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception: %s", exc)
    log.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc),
            "type": type(exc).__name__,
            "traceback": traceback.format_exc() if os.getenv('DEBUG') == '1' else None
        },
    )

@app.get('/health')
def health_check():
    return {'status': 'ok'}

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

class JobSubmissionRequest(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    uom_system: Optional[str] = 'US'
    measure_preference: Optional[str] = 'Volume'

class PaprikaIngestResponse(BaseModel):
    job_ids: List[str] = []
    recipe_ids: List[str] = []
    success_count: int = 0
    failure_count: int = 0
    errors: List[str] = []

class EmbedRequest(BaseModel):
    text: str = Field(..., description="The query string to vectorize for semantic search.")

class EmbedResponse(BaseModel):
    embedding: List[float]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upsert_ingestion_job(
    job_id: str,
    user_id: str,
    status: str,
    stage: str = 'IDLE',
    progress_pct: int = 0,
    recipe_count: int = 0,
    source_hint: Optional[str] = None,
    error_message: Optional[str] = None,
):
    import httpx
    supabase_url = os.getenv('SUPABASE_URL')
    service_key = os.getenv('SUPABASE_SERVICE_KEY')
    if not supabase_url or not service_key:
        log.warning("_upsert_ingestion_job: Missing Supabase config, skipping update.")
        return

    url = f"{supabase_url}/rest/v1/ingestion_jobs"
    headers = {
        'apikey': service_key,
        'Authorization': f'Bearer {service_key}',
        'Content-Type': 'application/json',
        'Prefer': 'resolution=merge-duplicates'
    }
    payload = {
        'id': job_id,
        'user_id': user_id,
        'status': status,
        'stage': stage,
        'progress_pct': progress_pct,
        'recipe_count': recipe_count,
        'source_hint': source_hint,
        'error_message': error_message,
        'updated_at': 'now()'
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
    except Exception as exc:
        log.warning('_upsert_ingestion_job failed (non-fatal): %s', exc)

def _get_client():
    from google import genai
    api_key = os.getenv('GOOGLE_API_KEY')
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set in environment.")
    return genai.Client(api_key=api_key)

def _fetch_user_axes_and_ids(user_id: str):
    try:
        source = SupabaseCategorySource()
        return source.load_axes(user_id), source.load_category_ids(user_id)
    except Exception as exc:
        log.warning('_fetch_user_axes_and_ids failed (non-fatal): %s', exc)
        return [], {}

def _background_url_ingestion(
    job_id: str,
    user_id: str,
    url: Optional[str],
    text: Optional[str],
    uom_system: str,
    measure_preference: str,
):
    log.info(f"[_background_url_ingestion] Job {job_id} started.")
    _upsert_ingestion_job(job_id, user_id, 'running', stage='LOADING', source_hint=url or "Text")
    
    recipe_id = str(uuid.uuid4())
    source_text = ""
    image_url = None

    try:
        if url:
            import httpx
            jina_url = f'https://r.jina.ai/{url}'
            headers = {
                'Accept': 'text/html',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            }
            jina_api_key = os.getenv('JINA_API_KEY')
            if jina_api_key:
                headers['Authorization'] = f'Bearer {jina_api_key}'
            
            with httpx.Client(timeout=30.0, follow_redirects=True) as http:
                resp = http.get(jina_url, headers=headers)
                resp.raise_for_status()
                raw_markdown = resp.text
                source_text = html_to_text(raw_markdown)
                
                candidate_img = _extract_image_url_from_markdown(raw_markdown)
                if candidate_img:
                    image_url = _upload_image_to_storage(image_url=candidate_img, user_id=user_id, recipe_id=recipe_id)
        else:
            source_text = text or ""

        if not source_text.strip():
            raise RuntimeError("No text content available for ingestion.")

        _upsert_ingestion_job(job_id, user_id, 'running', stage='EXTRACTING', progress_pct=30)
        
        client = _get_client()
        user_axes, category_ids = _fetch_user_axes_and_ids(user_id)
        
        result = run_cayenne_pipeline(
            source_text=source_text,
            client=client,
            uom_system=uom_system,
            measure_preference=measure_preference,
            source_url=url,
            image_url=image_url,
            user_axes=user_axes,
        )
        
        _upsert_ingestion_job(job_id, user_id, 'running', stage='EMBEDDING', progress_pct=80)
        
        write_recipe_to_supabase(
            result, user_id=user_id, recipe_id=recipe_id, category_ids=category_ids
        )
        
        _upsert_ingestion_job(job_id, user_id, 'done', stage='DONE', progress_pct=100, recipe_count=1)
        log.info(f"[_background_url_ingestion] Job {job_id} complete.")

    except Exception as e:
        log.error(f"[_background_url_ingestion] Job {job_id} failed: {e}")
        _upsert_ingestion_job(job_id, user_id, 'error', error_message=str(e))

def _background_paprika_ingestion(
    job_id: str,
    user_id: str,
    tmp_path: str,
    uom_system: str,
    measure_preference: str,
):
    log.info(f"[_background_paprika_ingestion] Job {job_id} started.")
    from recipeparser.io.readers.paprika import PaprikaReader
    
    try:
        user_axes, category_ids = _fetch_user_axes_and_ids(user_id)
        _upsert_ingestion_job(job_id, user_id, 'running', stage='LOADING', progress_pct=10)
        
        reader = PaprikaReader()
        results = reader.read_entries_with_images(tmp_path)
        total = len(results)
        log.info(f"[_background_paprika_ingestion] Found {total} entries.")

        client = _get_client()
        success_count = 0
        
        for i, res in enumerate(results):
            entry = res['recipe']
            image_bytes = res['image_bytes']
            recipe_name = entry.get('name', 'Untitled')
            
            current_pct = int(10 + (i / total) * 85)
            _upsert_ingestion_job(
                job_id, user_id, 'running', 
                stage='EXTRACTING', 
                progress_pct=current_pct,
                recipe_count=success_count
            )
            
            try:
                recipe_id = str(uuid.uuid4())
                image_url = None
                if image_bytes:
                    image_url = _upload_image_to_storage(
                        image_url=None, 
                        user_id=user_id, 
                        recipe_id=recipe_id, 
                        image_bytes=image_bytes
                    )

                source_text = f"Title: {recipe_name}\\n\\nIngredients:\\n{entry.get('ingredients', '')}\\n\\nDirections:\\n{entry.get('directions', '')}"
                
                result = run_cayenne_pipeline(
                    source_text=source_text,
                    client=client,
                    uom_system=uom_system,
                    measure_preference=measure_preference,
                    source_url=entry.get('source_url'),
                    image_url=image_url,
                    user_axes=user_axes
                )
                
                write_recipe_to_supabase(
                    result, user_id=user_id, recipe_id=recipe_id, category_ids=category_ids
                )
                success_count += 1
            except Exception as e:
                log.error(f"Error processing Paprika entry '{recipe_name}': {e}")

        _upsert_ingestion_job(job_id, user_id, 'done', stage='DONE', progress_pct=100, recipe_count=success_count)
        log.info(f"[_background_paprika_ingestion] Job {job_id} complete. Success: {success_count}")

    except Exception as e:
        log.error(f"[_background_paprika_ingestion] Job {job_id} failed: {e}")
        _upsert_ingestion_job(job_id, user_id, 'error', error_message=str(e))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

def _background_file_ingestion(
    job_id: str,
    user_id: str,
    tmp_path: str,
    uom_system: str,
    measure_preference: str,
):
    """
    Background worker for PDF and EPUB ingestion.
    Supports multi-recipe extraction and image marker processing.
    """
    log.info(f"[_background_file_ingestion] Job {job_id} started for {tmp_path}")
    suffix = Path(tmp_path).suffix.lower()
    
    try:
        user_axes, category_ids = _fetch_user_axes_and_ids(user_id)
        _upsert_ingestion_job(job_id, user_id, 'running', stage='LOADING', progress_pct=10)
        
        output_dir = tempfile.mkdtemp()
        try:
            if suffix == '.pdf':
                from recipeparser.io.readers.pdf import load_pdf
                source_name, image_dir, qualifying_images, chunks = load_pdf(tmp_path, output_dir)
            elif suffix == '.epub':
                from recipeparser.io.readers.epub import load_epub
                source_name, image_dir, qualifying_images, chunks = load_epub(tmp_path, output_dir)
            else:
                raise RuntimeError(f"Unsupported file type: {suffix}")

            total_chunks = len(chunks)
            log.info(f"[_background_file_ingestion] Loaded {total_chunks} chunks from {source_name}")
            
            client = _get_client()
            success_count = 0
            
            # For PDF/EPUB, we process chunks (pages/chapters).
            # Each chunk is passed through the full run_cayenne_pipeline which
            # handles extraction, refinement, categorisation, and embedding in
            # one call.  Chunks that don't contain a recognisable recipe are
            # skipped (pipeline raises ValueError).
            for i, chunk in enumerate(chunks):
                current_pct = int(10 + (i / total_chunks) * 85)
                _upsert_ingestion_job(
                    job_id, user_id, 'running', 
                    stage='CHUNKING', 
                    progress_pct=current_pct,
                    recipe_count=success_count
                )
                
                try:
                    recipe_id = str(uuid.uuid4())
                    result = run_cayenne_pipeline(
                        source_text=chunk,
                        client=client,
                        uom_system=uom_system,
                        measure_preference=measure_preference,
                        source_url=source_name,
                        image_url=None,  # image handling per-chunk not yet supported
                        user_axes=user_axes,
                    )
                    write_recipe_to_supabase(
                        result, user_id=user_id, recipe_id=recipe_id, category_ids=category_ids
                    )
                    success_count += 1
                        
                except ValueError:
                    # Chunk contained no recognisable recipe — skip silently
                    log.info(f"Chunk {i+1}/{total_chunks}: no recipe found, skipping.")
                except Exception as chunk_err:
                    log.error(f"Error processing chunk {i+1}: {chunk_err}")

            _upsert_ingestion_job(job_id, user_id, 'done', stage='DONE', progress_pct=100, recipe_count=success_count)
            log.info(f"[_background_file_ingestion] Job {job_id} complete. Success: {success_count}")

        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    except Exception as e:
        log.error(f"[_background_file_ingestion] Job {job_id} failed: {e}")
        log.error(traceback.format_exc())
        _upsert_ingestion_job(job_id, user_id, 'error', error_message=str(e))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

def _extract_image_url_from_markdown(markdown: str) -> Optional[str]:
    # og:image / twitter:image meta lines take priority over inline Markdown images
    match = re.search(r'(?:og:image|twitter:image)["\s:]+\s*(https?://\S+)', markdown, re.IGNORECASE)
    if match:
        url = match.group(1)
        # Strip one trailing ')' if the URL ends with '))' (double-paren artifact)
        if url.endswith('))'):
            url = url[:-1]
        return url
    # Fall back to the first Markdown image tag
    match = re.search(r'!\[.*?\]\((https?://\S+)\)', markdown)
    if match: return match.group(1)
    return None

def _upload_image_to_storage(
    image_url: Optional[str], 
    user_id: str, 
    recipe_id: str,
    image_bytes: Optional[bytes] = None
) -> Optional[str]:
    import httpx
    supabase_url = os.getenv('SUPABASE_URL')
    service_key = os.getenv('SUPABASE_SERVICE_KEY')
    if not supabase_url or not service_key: return None

    content_type = 'image/jpeg'
    
    if image_bytes:
        pass
    elif image_url:
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as http:
                resp = http.get(image_url)
                resp.raise_for_status()
                image_bytes = resp.content
                content_type = resp.headers.get('content-type', 'image/jpeg').split(';')[0].strip()
        except Exception as exc:
            log.warning('Image download failed (%s): %s', image_url, exc)
            return None
    else:
        return None

    ext = { 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif' }.get(content_type, '.jpg')
    storage_path = f'{user_id}_{recipe_id}{ext}'
    upload_url = f'{supabase_url}/storage/v1/object/recipe-images/{storage_path}'
    
    headers = { 'apikey': service_key, 'Authorization': f'Bearer {service_key}', 'Content-Type': content_type, 'x-upsert': 'true' }
    try:
        with httpx.Client(timeout=30.0) as http:
            up_resp = http.post(upload_url, content=image_bytes, headers=headers)
            up_resp.raise_for_status()
    except Exception as exc:
        log.warning('Image upload failed: %s', exc)
        return None

    return f'{supabase_url}/storage/v1/object/public/recipe-images/{storage_path}'

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post('/jobs', response_model=JobResponse, status_code=202)
async def submit_job(
    request: JobSubmissionRequest,
    background_tasks: BackgroundTasks,
    _user: dict = Depends(_verify_supabase_jwt),
):
    user_id = _user.get('sub', 'unknown')
    job_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    _upsert_ingestion_job(job_id, user_id, 'pending', source_hint=request.url or "Text")
    background_tasks.add_task(_background_url_ingestion, job_id, user_id, request.url, request.text, request.uom_system or 'US', request.measure_preference or 'Volume')
    return JobResponse(job_id=job_id, recipe_id=recipe_id)

@app.post('/jobs/file', response_model=JobResponse, status_code=202)
async def submit_file_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    uom_system: str = Form('US'),
    measure_preference: str = Form('Volume'),
    _user: dict = Depends(_verify_supabase_jwt),
):
    user_id = _user.get('sub', 'unknown')
    job_id = str(uuid.uuid4())
    # No recipe_id — file uploads may contain many recipes; each gets its own UUID internally.

    suffix = os.path.splitext(file.filename or "")[1]
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, 'wb') as tmp:
        shutil.copyfileobj(file.file, tmp)

    _upsert_ingestion_job(job_id, user_id, 'pending', source_hint=file.filename or "File")

    if suffix.lower() == '.paprikarecipes':
        background_tasks.add_task(_background_paprika_ingestion, job_id, user_id, tmp_path, uom_system, measure_preference)
    else:
        background_tasks.add_task(_background_file_ingestion, job_id, user_id, tmp_path, uom_system, measure_preference)

    return JobResponse(job_id=job_id)

@app.post('/ingest/paprika', response_model=JobResponse, status_code=202)
async def ingest_paprika(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    uom_system: str = Form('US'),
    measure_preference: str = Form('Volume'),
    _user: dict = Depends(_verify_supabase_jwt),
):
    return await submit_file_job(background_tasks, file, uom_system, measure_preference, _user)

# ---------------------------------------------------------------------------
# Legacy synchronous endpoints (used by tests and direct integrations)
# These run the pipeline synchronously and return { job_id, recipe_id }.
# ---------------------------------------------------------------------------

class IngestTextRequest(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    uom_system: Optional[str] = 'US'
    measure_preference: Optional[str] = 'Volume'


class IngestUrlRequest(BaseModel):
    url: str
    uom_system: Optional[str] = 'US'
    measure_preference: Optional[str] = 'Volume'


@app.post('/ingest', response_model=JobResponse, status_code=202)
async def ingest_text(
    request: IngestTextRequest,
    _user: dict = Depends(_verify_supabase_jwt),
):
    """Synchronous text ingestion. Accepts { text } only (url not yet supported here)."""
    user_id = _user.get('sub', 'unknown')

    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail='text field is required and must not be empty.')

    job_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())

    try:
        client = _get_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    user_axes, category_ids = _fetch_user_axes_and_ids(user_id)

    try:
        result = run_cayenne_pipeline(
            source_text=request.text,
            client=client,
            uom_system=request.uom_system or 'US',
            measure_preference=request.measure_preference or 'Volume',
            source_url=None,
            image_url=None,
            user_axes=user_axes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    write_recipe_to_supabase(result, user_id=user_id, recipe_id=recipe_id, category_ids=category_ids)

    return JobResponse(job_id=job_id, recipe_id=recipe_id)


@app.post('/ingest/pdf', response_model=JobResponse, status_code=202)
async def ingest_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    uom_system: str = Form('US'),
    measure_preference: str = Form('Volume'),
    _user: dict = Depends(_verify_supabase_jwt),
):
    """
    PDF ingestion — fire-and-forget background task.
    PDFs may contain many recipes; each is extracted and written with its own UUID.
    Returns only job_id (no recipe_id) — recipes arrive via PowerSync.
    """
    user_id = _user.get('sub', 'unknown')

    filename = file.filename or ''
    if not filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail='Only .pdf files are accepted.')

    pdf_bytes = await file.read()

    # Validate it is a real PDF before queuing the background task
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        if doc.page_count == 0:
            raise ValueError('PDF has no pages.')
    except Exception:
        raise HTTPException(status_code=422, detail='File could not be parsed as a valid PDF.')

    # Write to a temp file for the background worker
    fd, tmp_path = tempfile.mkstemp(suffix='.pdf')
    with os.fdopen(fd, 'wb') as f:
        f.write(pdf_bytes)

    job_id = str(uuid.uuid4())
    _upsert_ingestion_job(job_id, user_id, 'pending', source_hint=filename)
    background_tasks.add_task(_background_file_ingestion, job_id, user_id, tmp_path, uom_system, measure_preference)

    # No recipe_id — N recipes will be written by the background worker
    return JobResponse(job_id=job_id)


@app.post('/ingest/url', response_model=JobResponse, status_code=202)
async def ingest_url(
    request: IngestUrlRequest,
    _user: dict = Depends(_verify_supabase_jwt),
):
    """Synchronous URL ingestion via Jina reader."""
    user_id = _user.get('sub', 'unknown')

    if not request.url or not request.url.strip():
        raise HTTPException(status_code=400, detail='url field is required and must not be empty.')

    job_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())

    # Fetch page via Jina
    import httpx as _httpx
    jina_url = f'https://r.jina.ai/{request.url}'
    try:
        async with _httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            resp = await http.get(jina_url)
            resp.raise_for_status()
            raw_markdown = resp.text
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f'Failed to fetch URL: {exc}')

    source_text = html_to_text(raw_markdown)

    # Validate it looks like a recipe
    from recipeparser.io.readers.epub import is_recipe_candidate
    if not is_recipe_candidate(source_text):
        raise HTTPException(status_code=422, detail='Page does not appear to contain a recipe.')

    image_url = _extract_image_url_from_markdown(raw_markdown)
    if image_url:
        image_url = _upload_image_to_storage(image_url=image_url, user_id=user_id, recipe_id=recipe_id)

    try:
        client = _get_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    user_axes, category_ids = _fetch_user_axes_and_ids(user_id)

    try:
        result = run_cayenne_pipeline(
            source_text=source_text,
            client=client,
            uom_system=request.uom_system or 'US',
            measure_preference=request.measure_preference or 'Volume',
            source_url=request.url,
            image_url=image_url,
            user_axes=user_axes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    write_recipe_to_supabase(result, user_id=user_id, recipe_id=recipe_id, category_ids=category_ids)

    return JobResponse(job_id=job_id, recipe_id=recipe_id)


@app.post('/embed', response_model=EmbedResponse)
async def embed_text(request: EmbedRequest, _user: dict = Depends(_verify_supabase_jwt)):
    try:
        client = _get_client()
        from recipeparser.gemini import get_embeddings
        return EmbedResponse(embedding=get_embeddings(request.text, client))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
