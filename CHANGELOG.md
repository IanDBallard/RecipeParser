# Changelog

All notable changes to RecipeParser are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [6.0.0] — 2026-03-20

### 🐛 Bug Fixes

- **`_select_reader()` unreachable branch** (`recipeparser/adapters/api.py`) — the `elif media_type == "application/epub+zip"` branch was dead code because the preceding `if` block already handled EPUBs and returned early. The branch has been restructured so all media-type routing is reachable and exercised by tests.
- **Deprecated `response_schema` + `response.parsed`** (`recipeparser/gemini.py`) — calls to the Gemini SDK were using the deprecated `response_schema` config key and `response.parsed` accessor, which are removed in `google-genai >= 1.38.0`. Updated to use `config=types.GenerateContentConfig(response_mime_type="application/json", ...)` and `json.loads(response.text)` respectively.

### 🧪 Testing

- **73 tests, 0 failures** — regression tests added for both bug fixes:
  - `tests/unit/test_select_reader.py` — covers all `_select_reader()` branches (EPUB, PDF, text, unknown media type, missing content-type)
  - `tests/test_gemini.py` — covers `generate_content` call signature, JSON parsing path, and schema passthrough

---

## [5.0.0] — 2026-03-17

### 💥 Breaking Changes

- **Layered architecture refactor** — the monolithic `recipeparser/` flat layout has been replaced with a clean three-layer structure. Any code importing directly from old module paths must be updated:
  - `recipeparser.gemini` → `recipeparser.core.engine` (orchestration) / `recipeparser.core.providers` (LLM/embedding ABCs)
  - `recipeparser.pipeline` → `recipeparser.core.fsm` (FSM) + `recipeparser.core.engine` (pure logic)
  - `recipeparser.supabase_writer` → `recipeparser.io.writers.supabase`
  - Category sources: `recipeparser.io.category_sources.{yaml_source,paprika_db,supabase_source}`
- **`POST /ingest` replaced by `POST /jobs`** — the API now uses a fire-and-forget job pattern. The endpoint returns `202 Accepted` with `{ "job_id": "uuid" }` immediately; the completed recipe is written directly to Supabase by the worker. Callers must poll `GET /jobs/{job_id}` for status.
- **`categories` field removed from `CayenneRecipe`** — category assignment is now handled by the multipolar grid system and written to the `recipe_categories` junction table in Supabase. The flat `List[str]` field is no longer returned in the API response.

### ✨ New Features

#### Multipolar Grid Categorization
- Recipes are now categorized against a **user-defined set of axes** (e.g., "Cuisine", "Protein", "Meal Type"), each with its own list of valid tags.
- The LLM receives a **dynamically generated Pydantic schema** (via `create_model()`) that enforces the exact tag vocabulary per axis — hallucinated tags are structurally impossible.
- **Zero-tag mandate**: the LLM returns `[]` for any axis that doesn't apply to the recipe; a post-validation pass strips any tags that slipped through.
- **0–2 tags per axis** — recipes are never over-categorized; the constraint is enforced both in the prompt and in the response schema.
- Categorization is merged into the existing **refinement pass** (Fat Tokens + UOM + Categories in a single Gemini call), eliminating a separate API round-trip.
- Results are written to the `recipe_categories` junction table in Supabase, partitioned by `user_id` for PowerSync compatibility.

#### `CategorySource` ABC (Pluggable Taxonomy)
- New abstract base class `recipeparser.io.category_sources.base.CategorySource` with a single `load() -> MultipolarGrid` method.
- Three built-in implementations:
  - `YamlCategorySource` — loads axes + tags from a local `categories.yaml` file (default for CLI/GUI)
  - `PaprikaDbCategorySource` — reads live taxonomy from Paprika 3's SQLite database
  - `SupabaseCategorySource` — fetches the authenticated user's category tree from Supabase (used by the API adapter)
- The engine accepts any `CategorySource` implementation — new sources can be added without touching core logic.

#### Layered Architecture (`recipeparser/core/` + `recipeparser/io/`)
- **`recipeparser/core/engine.py`** — pure `RecipeEngine` orchestrator with zero I/O; accepts reader, writer, and category source as injected dependencies.
- **`recipeparser/core/fsm.py`** — `ExtractionFSM` state machine (externalized, observable); fires callbacks on every state transition for adapter-level progress reporting.
- **`recipeparser/core/providers/`** — `LLMProvider` and `EmbeddingProvider` ABCs with a `GeminiProvider` implementation; swappable without touching the engine.
- **`recipeparser/io/readers/`** — `EpubReader`, `PdfReader`, `UrlReader`, `PaprikaReader` (source adapters).
- **`recipeparser/io/writers/`** — `SupabaseWriter`, `CayenneZipWriter`, `PaprikaZipWriter` (output adapters).
- **`recipeparser/adapters/`** — thin CLI, GUI, and API wrappers that wire readers/writers/sources to the engine.

#### Fire-and-Forget Job API (`recipeparser/adapters/api.py`)
- `POST /jobs` — accepts `{ url?, text?, uom_system?, measure_preference? }`, enqueues a background worker, returns `202 { "job_id": "uuid" }` immediately.
- `GET /jobs/{job_id}` — returns current job status: `pending | running | done | error`, FSM stage, `progress_pct`, `recipe_count`, and `error_message`.
- Job state is written to the `ingestion_jobs` table in Supabase; PowerSync syncs it to the mobile app in real time — zero polling from the client.
- `.env` is excluded from the Docker image (`.dockerignore` updated); `DISABLE_AUTH=1` environment variable added for CI test jobs.

#### Live End-to-End Test Suites
- Three standalone live E2E scripts (excluded from standard `pytest` run; require a running Docker server):
  - `tests/live_api_test.py` — exercises `POST /jobs` + `GET /jobs/{id}` against a live container
  - `tests/live_cli_test.py` — runs the CLI adapter end-to-end with a real Gemini API call
  - `tests/live_gui_test.py` — drives the GUI adapter headlessly through a full parse run
- `pyproject.toml` updated: `python_files = ["test_*.py"]` ensures `live_*` scripts are never picked up by the standard test runner.

### 🔧 Improvements

- **`toc.py` bare `Link` crash fixed** — `toc.py` now handles EPUB `Link` nodes that have no `title` attribute without raising `AttributeError`.
- **Docker `.env` exclusion** — `.dockerignore` updated to prevent `.env` from being baked into the image; secrets are injected at runtime via environment variables.
- **`DISABLE_AUTH` CI flag** — GitHub Actions CI test job sets `DISABLE_AUTH=1` so the containerised API accepts unauthenticated requests during automated testing without requiring a live Supabase JWT secret.

### 🧪 Testing

- **384 tests, 0 failures** (up from 356 in v3.0.0)
- New test coverage:
  - Multipolar grid schema generation and zero-tag validation
  - `CategorySource` ABC implementations (YAML, Paprika DB, Supabase)
  - Fire-and-forget job API (`POST /jobs`, `GET /jobs/{id}`, background worker lifecycle)
  - `RecipeEngine` with injected mock dependencies (pure unit tests, zero I/O)
  - `ExtractionFSM` state transition invariants
- **10/10 live E2E tests passing** against a running Docker container (API, CLI, GUI adapters)

### 📦 Architecture Summary

```
recipeparser/
├── core/
│   ├── engine.py          ← RecipeEngine orchestrator (pure — no I/O)
│   ├── fsm.py             ← ExtractionFSM state machine
│   └── providers/         ← LLMProvider + EmbeddingProvider ABCs + GeminiProvider
├── io/
│   ├── readers/           ← EpubReader, PdfReader, UrlReader, PaprikaReader
│   ├── writers/           ← SupabaseWriter, CayenneZipWriter, PaprikaZipWriter
│   └── category_sources/  ← CategorySource ABC + YAML / PaprikaDB / Supabase impls
└── adapters/              ← CLI, GUI, API thin wrappers
```

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
