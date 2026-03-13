# Changelog

All notable changes to RecipeParser are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [3.0.0] — 2026-03-12

### ✨ New Features

#### Cayenne Ingestion API (`recipeparser/api.py`)
- New **FastAPI service** exposing two endpoints designed for the Project Cayenne mobile app:
  - `POST /ingest` — full 3-step pipeline: extract recipes from raw text or PDF → refine into structured Cayenne schema → embed with `text-embedding-004`
  - `POST /embed` — standalone query vectorisation for semantic search
- **Supabase JWT authentication** (HS256 via PyJWT) on all endpoints; unauthenticated requests are rejected with `401`
- URL ingestion reserved (`400 Not Yet Implemented`) — groundwork laid for Phase 2

#### Cayenne Refinement Pass (`recipeparser/gemini.py`)
- New `refine_recipe_for_cayenne()` function powered by **Gemini 2.5 Flash** (upgraded from 2.0 Flash for native thinking support)
- Produces fully structured `CayenneRecipe` output:
  - `StructuredIngredient` list with `id`, `amount`, `unit`, `name`, `fallback_string`, `converted_amount`, `converted_unit`, `is_ai_converted`
  - `TokenizedDirection` list using **Fat Token** format (`{{ing_01|fallback text}}`) — ingredient references embedded directly in direction text for deterministic math-scaling
  - AI-powered Volume-to-Weight conversion flagged with `is_ai_converted` for UI transparency
- New `get_embeddings()` using `text-embedding-004` (1536-dimensional vectors, compatible with `pgvector` / `sqlite-vec`)

#### Pipeline Resumability (`recipeparser/pipeline.py`)
- **Checkpoint persistence** — pipeline state (completed segment indices) saved to `<output_dir>/.recipeparser_checkpoints/<book_hash>.json` after each segment; automatically resumed on re-run of the same book
- **Cooperative pause/resume** — `PipelineController.check_pause_point()` called between segments; orchestrator-level pause guard handles the race condition where a worker transitions `PAUSING → PAUSED` before the orchestrator checks
- **FSM correctness fix** — `transition("done")` now called at end of `process_epub` so the controller correctly reaches `IDLE` on successful completion
- **Rate-limit auto-pause** — `PipelineController` tracks RPM consumption and automatically pauses + resumes when the Gemini free-tier window resets

### 🔧 Improvements

- **Gemini 2.5 Flash** used for the refinement pass (was 2.0 Flash); native thinking mode improves structured output accuracy
- **Docker smoke test** (`tests/smoke_test_docker.py`) added to CI; validates the containerised API starts and responds correctly
- **Dockerfile dependencies** updated to match `requirements.txt` (FastAPI, Uvicorn, HTTPx, PyJWT)

### 🧪 Testing

- **356 tests, 0 failures** (up from 350 in v2.2.0)
- New test modules:
  - `tests/test_api.py` — 43 tests covering `/ingest` and `/embed` endpoints, auth, error paths, schema validation, and UOM passthrough
  - `tests/test_gemini_cayenne.py` — 4 tests for `get_embeddings` and `refine_recipe_for_cayenne`
  - `tests/test_gui.py` — 6 tests for `_parse_run_config` logic (free-tier / paid-tier concurrency rules)
  - `tests/test_pipeline_resumability.py` — 3 integration tests: checkpoint save/load, cancel, and pause/resume
- **Headless GUI test support** — `conftest.py` now injects lightweight `tkinter` / `customtkinter` stubs into `sys.modules` when the C extension is unavailable (e.g. PlatformIO's embedded Python), allowing GUI logic tests to run in any environment without a display

### 🔒 Security

- API key (`GOOGLE_API_KEY`) never exposed in responses or logs
- Supabase JWT secret validated server-side; all ingestion requests require a valid bearer token

### 📦 Dependencies Added

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | ≥ 0.115.0 | Cayenne Ingestion API |
| `uvicorn` | ≥ 0.30.0 | ASGI server for FastAPI |
| `httpx` | ≥ 0.27.0 | Async HTTP client (test client) |
| `PyJWT` | ≥ 2.8.0 | Supabase JWT verification |

---

## [2.2.0] — 2026-03-08

### ✨ New Features

- **Folder processing** (`recipeparser folder <dir>`) — batch-process all EPUBs and PDFs in a directory
- **`PipelineController` FSM** — Finite State Machine wrapping the pipeline with states `IDLE → RUNNING → PAUSING → PAUSED → RESUMING → RUNNING → DONE`; GUI Pause/Resume/Cancel buttons wired to FSM transitions
- **Rate-limit auto-pause** — when RPM budget is exhausted, pipeline automatically pauses and resumes after the Gemini rate-limit window resets (no manual intervention required)
- **`recategorize` command** — re-run AI categorisation on an existing `.paprikarecipes` export without re-parsing; produces a new archive with updated categories
- **Export merge** (`recipeparser merge`) — deduplicate and merge multiple `.paprikarecipes` archives into one; accent- and case-insensitive deduplication

### 🔧 Improvements

- `PipelineController` checkpoint subdir renamed to `.recipeparser_checkpoints` (hidden directory)
- GUI concurrency spinner disabled when free-tier checkbox is active
- CLI `--concurrency` clamped to 1–10; `--rpm` passed through to rate limiter

### 🧪 Testing

- 350 tests, 0 failures
- New: `test_pipeline_controller.py` (561 lines), `test_merge_exports.py`, `test_recategorize.py`, `test_cli.py` expansions

---

## [2.1.0] — 2026-02-xx

### ✨ New Features

- **PDF support** — text-based PDFs extracted via PyMuPDF; scanned PDFs fall back to Gemini Vision OCR (page-by-page)
- **TOC extraction** — programmatic EPUB/PDF table of contents used to segment books by recipe title; AI TOC classification fallback when no programmatic TOC is available
- **Recon report** — post-run reconciliation compares TOC entries against extracted recipe names; highlights missed or extra recipes
- **Run summary** — printed at end of each run: total segments, extracted recipes, skipped segments, elapsed time

---

## [2.0.x] — 2026-01-xx

### 2.0.6
- RPM rate limit (`--rpm`) and concurrency cap (`--concurrency`) CLI flags
- Free-tier GUI checkbox (5 req/min, concurrency=1)

### 2.0.5
- First fully-tested 4-job CI pipeline: test → build → smoke-test → release
- GitHub Actions builds Windows installer automatically on `v*` tag push

### 2.0.4
- GitHub Actions automated installer build

### 2.0.3
- Build requires python.org Python (tkinter bundled)

### 2.0.2
- Fix customtkinter packaging for PyInstaller

### 2.0.1
- User data stored in writable paths (`%APPDATA%` / `~/.local/share`)
- Minimal default `categories.yaml` shipped with installer

### 2.0.0
- **Paprika DB category sync** — `recipeparser --sync-categories` reads live taxonomy from Paprika 3's SQLite database
- GUI Categories tab with two-panel editor (parent / subcategory)
- CLI `--sync-categories` flag

---

## [0.2.0] — 2025-12-xx

- CustomTkinter GUI with Parse tab, log panel, progress bar, Pause/Cancel controls
- Windows installer (Inno Setup + PyInstaller)

---

## [0.1.0] — 2025-11-xx

- Initial working implementation: EPUB → Paprika 3 recipe export
- Parallel extraction with `ThreadPoolExecutor`
- Category taxonomy via `categories.yaml`
- Hero image injection into Paprika export
- Calibre folder path support
