# Changelog

All notable changes to RecipeParser are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [8.0.0] — 2026-09-12

The recipe edit backend, the recipe source citation, and everything that landed between them: 47 commits over fourteen pull requests (#24–#37) since v7.0.0, plus #38 and #39.

### ⚠️ Requires — apply the Cayenne migrations **before** deploying this version
`SupabaseWriter` puts every new column into **every** recipe INSERT, unconditionally and behind no feature flag. Against a schema missing any of them PostgREST rejects the row with `PGRST204` ("column … does not exist") and `write_recipe_to_supabase` raises `RuntimeError`, so every ingest fails, for every user, on every path — URL, file, and Paprika alike. `REGEN_WORKER_ENABLED` does **not** protect the writer; leaving the flag unset changes nothing here.

- **Cayenne migration 013 (`recipe_edit_columns`)** — `ingredient_lines`, `direction_steps`, `body_rev`, `derived_rev`, `amount_overrides`, and the nine `prep_*` / `cook_*` / `servings_*` duration columns.
- **Cayenne migration `recipe_source_citation` (Cayenne PR #58)** — `source_kind`, `source_key`, `source_title`, `source_author`.
- **Cayenne migration 014 (`regen_rpcs`)** is required in addition before setting `REGEN_WORKER_ENABLED=1`: `claim_stale_recipes` and `regen_failed` do not exist without it and every poll raises. The RPCs' required semantics are in `docs/sql/regen-rpcs.md`; they were implemented, verified against a real Postgres, and applied 2026-09-09 (#27).
- **The live API's environment must carry both `REGEN_WORKER_ENABLED=1` and the service-role key, or the process will not come up** (#34). With the flag set and no service client the server refuses to start with a `RuntimeError` naming `SUPABASE_URL` and the key, instead of answering every request while draining nothing; with the flag unset it warns.

All three migrations are applied to the live project (013 and 014 on 2026-09-09, the citation columns on 2026-09-12). The warnings stand for every other environment.

### ✨ Added — recipe edit backend (#26, #27, #32, #34)
- `StructuredIngredient.line_index`, emitted by REFINE and normalised (in range, unique). The field is optional by design, so an out-of-range or duplicated index degrades that entry to `null` with a warning rather than failing the recipe; the client falls back to `fallback_string` matching (spec 4.3).
- `core/durations.py`: deterministic duration and servings parser; shared fixture `tests/fixtures/duration_cases.json`, byte-identical with the Cayenne client's copy, including the `"2.5 min"` → 2 case that pins round-half-even on both sides.
- Raw `ingredient_lines` / `direction_steps` and structured duration/servings columns carried through ASSEMBLE and written by `SupabaseWriter`.
- `RegenWorker` and `RecatWorker` background workers behind `REGEN_WORKER_ENABLED`, started from the FastAPI lifespan.
- `gemini.categorize_batch()` — categorise-only call for bulk recategorise.
- Ingestion reads `uom_system` / `measure_preference` from `profiles`; request values are the fallback.
- **`/health` publishes `regen_workers`** — `disabled` (the flag is not truthy), `misconfigured` (the flag is on but no service-role client resolves — the silent failure worth naming), or `started`. It initialises to `disabled` at module scope, so a server whose lifespan never ran never claims to have started workers. An empty regen queue looks identical whether the workers are running or were never started, so the state is published rather than inferred, the same argument `auth_mode` already makes (#32).
- `scripts/backfill_durations.py` one-off backfill. Run `--live` 2026-09-10 over 788 rows: `cook_min_minutes` 0 → 199, `prep_min_minutes` 0 → 3, `servings_min` 0 → 69.
- `docs/sql/regen-rpcs.md`: the required semantics and reference SQL for `claim_stale_recipes` / `regen_failed` — the 20-second quiet window, the 5-minute lease, three failures per `body_rev` — restated as what the implementation must keep satisfying now that it exists (#27).

### ✨ Added — recipe source citation (#36)
- Four citation columns — `source_kind`, `source_key`, `source_title`, `source_author` — written on every insert, derived reader-first in `core/citation.py` (`resolve_citation`): a book or an unknown book is settled entirely by the reader (EPUB/PDF metadata); a page keeps its host as the key and takes the site's stated name and byline from the model; with nothing known from the reader, the model's stated source is classified by the same seven rules the backfill uses.
- The EPUB and PDF readers no longer write `"Title — Author"` into `source_url`; the string form stays only as `Citation.display()`, used by the Paprika export.
- `Chunk.citation`, carried alongside `source_url` from every reader through to `assemble()`.
- The extraction model states `stated_source` and `byline` (both `repr=False`, so the refine prompt and the golden corpus are unchanged) and is told never to name itself as the source.
- `scripts/backfill_sources.py` — the seven classification rules applied to existing rows, per-rule counts reported, dry run by default, `--live` writes; `--force` reclassifies rows whose `source_kind` is already set (it overwrites a cook's Set source, so use it knowingly).
- `scripts/restore_source_urls.py` — restores the Paprika archive's per-entry clip URLs that the bulk import dropped, matched by title with `backfill_paprika_metadata`'s rule; ambiguous and unmatched entries are reported, never written. Dry run by default.
- Shared key fixture `tests/fixtures/citation_keys.json`, the contract `normalise_key` and its Cayenne client twin (`cayenne-web/src/lib/domain/citation.ts`) must both satisfy.

### ✨ Added — REFINE emits the other measure for every quantified line (#29)
- One rule in `build_refine_prompt`: always give the equivalent in the other measure, for every quantified volume-or-weight line, **regardless of the cook's Measure Preference**. Previously `converted_*` was filled only when the preference was Weight and the source was Volume, so a line a cook later re-entered in the other measure permanently lost its volume rendering for every reader. No schema change — `converted_amount`, `converted_unit` and `is_ai_converted` already exist — and no backfill: existing rows keep what they have until next regenerated. Only the refine-prompt snapshot moved; the recorded Gemini replies are unchanged.

### ✨ Added — one-off scripts, each already run against the live library
- `scripts/backfill_paprika_metadata.py` (#24) — fills `source`, `notes`, `rating`, `nutritional_info`, `description` and `difficulty` from a Paprika archive onto rows that predate migration 012. Titles are the only key, compared on letters and digits alone; ambiguity is skipped, nulls only, dry run by default, `--archive` and `--user-id` required. Run 2026-09-08: 743 of 788 rows filled, 9 ambiguous, 68 archive entries with no row in the library (reported, never created).
- `scripts/backfill_recipe_images.py` (#28) — uploads the photographs a Paprika archive carries for recipes ingested before the image path existed. Run 2026-09-08: 2 → 417 of 788 recipes with an image; 17 blocked on duplicated titles.
- `scripts/measure_match_band.py` (#31) — **read-only**; measures the cosine band a corpus and embedding model actually produce, so Cayenne's match-strength bar has a floor and ceiling that were measured rather than guessed. Deterministic (`--seed`), twenty fixed queries. Measured 2026-09-10 against `gemini-embedding-001` at 1536 dimensions over 788 recipes: `MATCH_FLOOR = 0.535548`, `MATCH_CEILING = 0.837484`. Committed because the constants expire with the model, the dimensionality, or the embedded text.

### 🐛 Fixed
- **The unparseable-duration fallback tidies its note** (`core/durations.py`, #33) — `parse_duration` and `parse_servings` normalised every successful path but returned the raw input on failure, so a malformed source field reached `prep_note` / `cook_note` / `servings_note` verbatim. Three live rows carried 37,114, 10,242 and 5,562 characters of newline padding in **synced** columns, and the same leak was live on every ingest through `SupabaseWriter`. The fallback now collapses whitespace runs and nothing else — not `_normalise`, which also lowercases and rewrites fraction glyphs, and this note is display text. Worst note after: 60 characters.
- **The refine prompt no longer instructs the model to null a non-nullable boolean** (`gemini.py`, in #29) — `is_ai_converted` is `bool` and the schema handed to Gemini has no null variant, so "leave all three null" asked for exactly what the schema forbids.

### 🧪 Testing
- **The suite runs in parallel by default** (#25): `pytest-xdist` is declared in `pyproject.toml`, the worker count is capped at 6 and scales down on small hosts (xdist's own `auto` counts logical cores and was slower than serial at 16). `--record-gemini`, `--update-goldens` and `--snapshot-update` demote the run to serial, each for a stated reason, and say so.
- **The suite decides its own worker state** (#38): `tests/conftest.py` sets `REGEN_WORKER_ENABLED` empty for the session, so a developer's `.env` carrying `=1` no longer collides with the test-time refusal to build a live service client and fail seven `tests/test_api.py` tests that CI, having no `.env`, never sees.
- 970 passed at this version.

### 📝 Documentation
- The philosophy spec: the five repairs the recipe-edit seams design found and two they imply (#30), and `prep_time` / `cook_time` staying on the server as the duration text as imported, out of sync, never displayed (#35). Both copies — this repository's and Cayenne's — are byte-identical and merged together.
- The extraction goldens, recipe edit backend, and source citation plans carry `**Merged:**` headers recording what execution changed and why (#37, #39) — including the one deliberate override of the recipe edit plan (`line_index` degrades rather than raises) and the migration requirement that three documents had stated backwards until `5d07702`.

---

## [7.0.0] — 2026-09-08

### ⚠️ Model migration — action may be required
- **Generation model moved to `GEMINI_MODEL` and now defaults to
  `gemini-3.1-flash-lite`** (`recipeparser/config.py`) — `gemini-2.5-flash`
  retires **2026-10-16**, and anyone still deployed on v6.0.0 will start
  failing calls after that date. The model name was previously a literal
  repeated at nine call sites (`gemini.py`, `toc.py`, `categories.py`); it
  is now one constant, overridable via the `GEMINI_MODEL` env var without a
  code change. The embedding model gets the same treatment as
  `GEMINI_EMBEDDING_MODEL`, unchanged in value — it is not implicated in the
  retirement. **Upgrading to v7.0.0 is the fix; no other action needed
  unless you were pinning `GEMINI_MODEL` yourself.**

### Added
- Golden test suite (`tests/goldens/`): a seven-file real-input corpus, recorded
  Gemini replies replayed offline, and goldens for readers, prompts, schemas,
  stage parsing, and the full pipeline through both zip writers.

### Fixed
- **`split_large_chunk` falls back to single-newline splitting** (`recipeparser/io/readers/epub.py`) — it previously split only on blank lines, so EPUB chapter text, which carries single newlines, was never split and `MAX_CHUNK_CHARS` had no effect for EPUBs; one real chapter reached 138,167 characters against a 30,000 limit. Behaviour for text that already split on blank lines is unchanged.
- **`HTTP_TIMEOUT_SECS` is applied to Gemini calls** (`recipeparser/gemini.py`) — the constant was defined and documented but referenced nowhere, so `generate_content` had no timeout and a stalled call could hang indefinitely. Now passed as `http_options.timeout` (180 s, expressed as 180000 ms, the SDK's unit).
- **Transient server errors are retried** (`recipeparser/gemini.py`) — only `429`/quota errors went through the exponential back-off ladder; a `500`/`502`/`503`/`504`/`UNAVAILABLE`/`DEADLINE_EXCEEDED`/`INTERNAL` raised on the first attempt instead of retrying. Client errors still raise immediately, and client-side timeouts still raise on the first attempt by design.

### Changed
- `gemini.py` and `toc.py` build their prompts through named functions
  (`build_extract_prompt`, `build_refine_prompt`, `build_table_prompt`,
  `build_plain_text_prompt`, `build_toc_parse_prompt`,
  `build_toc_classify_prompt`). Behaviour is unchanged; the prompts are now
  snapshot-tested.
- **Thinking disabled by default on every Gemini call**
  (`GEMINI_THINKING_BUDGET`, defaults to `0`) — every call in this package is
  a bounded extraction/refinement/classification task with one correct
  answer, not open-ended reasoning, and thinking tokens bill at the output
  rate for no benefit here.
- **Every Gemini reply's `usage_metadata` is now logged**
  (`_log_usage_metadata` in `recipeparser/gemini.py`) — prompt, candidate,
  thinking and total token counts, tagged by call site (extraction,
  refinement, categorisation, TOC parsing, vision OCR, embeddings). Ingestion
  cost was previously only estimable from prompt length; real per-call
  numbers now reach the log.

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
