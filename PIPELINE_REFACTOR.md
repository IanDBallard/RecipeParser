# RecipeParser Pipeline Refactor Design Document

<!-- SECTION PLACEHOLDERS — filled in below -->

## 1. Architecture Overview

RecipeParser is refactored from a monolithic `process_epub()` function into a
**Hexagonal (Ports & Adapters) architecture** with a pure functional core,
pluggable I/O modules, and an FSM-controlled orchestrator.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          ADAPTERS LAYER                                  │
│  CLI (cli.py)  │  GUI (gui.py)  │  API (api.py)                         │
│  Wire up readers, writers, category sources, and call RecipePipeline     │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                            I/O LAYER                                     │
├──────────────────────────┬───────────────────────────────────────────────┤
│  io/readers/             │  io/writers/                                  │
│    epub.py               │    paprika.py      (→ .paprikarecipes ZIP)    │
│    pdf.py                │    cayenne_zip.py  (→ .cayenne ZIP)           │
│    url.py                │    supabase.py     (→ Supabase REST)          │
│    paprika.py            │                                               │
├──────────────────────────┼───────────────────────────────────────────────┤
│  io/category_sources/    │                                               │
│    yaml_source.py        │                                               │
│    paprika_source.py     │                                               │
│    supabase_source.py    │                                               │
└──────────────────────────┴───────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                           CORE LAYER  (pure — no I/O)                   │
│                                                                          │
│  core/pipeline.py   RecipePipeline orchestrator                         │
│  core/fsm.py        PipelineController (pause/resume/cancel/checkpoint)  │
│  core/rate_limiter.py  GlobalRateLimiter singleton                       │
│  core/stages/                                                            │
│    extract.py       text chunk → List[RecipeExtraction]                  │
│    refine.py        RecipeExtraction → RefinedRecipe (Fat Tokens + UOM)  │
│    categorize.py    RefinedRecipe → grid_categories dict                 │
│    embed.py         RefinedRecipe → List[float] (1536-dim)               │
│    assemble.py      RefinedRecipe + embedding → IngestResponse           │
│  core/engine.py     Pure helpers (deduplicate, title_case, etc.)         │
└──────────────────────────────────────────────────────────────────────────┘
```

### Design Principles

1. **Pure core** — `core/` has zero I/O imports. All side effects live in adapters or I/O layer.
2. **Pluggable I/O** — readers, writers, and category sources are interchangeable ABCs.
3. **FSM-controlled routing** — the pipeline FSM determines which stages run based on input type.
4. **Error boundaries** — each chunk is processed in an isolated try/except; one bad chunk never aborts the batch.
5. **Global rate limiting** — a process-level singleton enforces Gemini RPM across all concurrent jobs.
6. **$0 fast-path** — Cayenne-native Paprika imports skip all Gemini stages when `_cayenne_meta` is present.

### Design Checkpoints — §1
- [x] All `core/` modules import only from `core/` or stdlib (no `io/`, no `adapters/`)
- [x] All `io/` modules import only from `core/` or stdlib (no `adapters/`)
- [x] All `adapters/` modules import from `io/` and `core/` only
- [x] No circular imports between layers (verified with `pydeps` or `importlib`)

## 2. Layer Definitions

### 2.1 Adapters Layer (`recipeparser/adapters/`)

Thin wrappers that wire I/O modules to the pipeline. Each adapter:
- Instantiates the appropriate reader, writer, and category source
- Constructs a `RecipePipeline` with a `PipelineController`
- Calls `pipeline.run(chunks)` and passes results to the writer

| File | Adapter | Reader(s) | Writer(s) | Category Source |
|---|---|---|---|---|
| `api.py` | FastAPI | URL, PDF, EPUB, Paprika | SupabaseWriter | SupabaseCategorySource |
| `cli.py` | Click CLI | PDF, EPUB, Paprika | PaprikaWriter or CayenneZipWriter | YamlCategorySource or PaprikaCategorySource |
| `gui.py` | Tkinter/Qt | PDF, EPUB, Paprika | PaprikaWriter or CayenneZipWriter | YamlCategorySource or PaprikaCategorySource |

### 2.2 I/O Layer (`recipeparser/io/`)

#### Readers (`io/readers/`)

All readers implement the `RecipeReader` ABC:

```python
class RecipeReader(ABC):
    @abstractmethod
    def read(self, source: str) -> List[Chunk]:
        """Return a list of Chunk objects ready for pipeline processing."""
```

A `Chunk` carries:
```python
@dataclass
class Chunk:
    text: str                          # Raw text for EXTRACT stage
    source_url: Optional[str]          # Provenance
    image_url: Optional[str]           # Pre-resolved image URL (if any)
    image_bytes: Optional[bytes]       # Raw image bytes (Paprika entries)
    pre_parsed: Optional[IngestResponse]  # Set for _cayenne_meta fast-path
    pre_parsed_embedding: Optional[List[float]]  # Embedding from _cayenne_meta
    input_type: InputType              # URL | PDF | EPUB | PAPRIKA_LEGACY | PAPRIKA_CAYENNE
```

| Reader | `input_type` | Notes |
|---|---|---|
| `UrlReader` | `URL` | Fetches via `r.jina.ai`, returns 1 chunk |
| `PdfReader` | `PDF` | Returns N page-group chunks |
| `EpubReader` | `EPUB` | Returns N chapter chunks |
| `PaprikaReader` | `PAPRIKA_LEGACY` or `PAPRIKA_CAYENNE` | Detects `_cayenne_meta` per entry |

#### Writers (`io/writers/`)

All writers implement the `RecipeWriter` ABC:

```python
class RecipeWriter(ABC):
    @abstractmethod
    def write(self, recipes: List[IngestResponse], **kwargs) -> None:
        """Write recipes to the destination."""
```

| Writer | Output | Used By |
|---|---|---|
| `PaprikaWriter` | `.paprikarecipes` ZIP | CLI, GUI |
| `CayenneZipWriter` | `.cayenne` ZIP (with `_cayenne_meta`) | CLI, GUI (backup/restore) |
| `SupabaseWriter` | Supabase `recipes` + `recipe_categories` tables | API |

#### Category Sources (`io/category_sources/`)

All sources implement the `CategorySource` ABC (already exists):

```python
class CategorySource(ABC):
    def load_axes(self, user_id=None) -> Dict[str, List[str]]: ...
    def load_category_ids(self, user_id=None) -> Dict[str, str]: ...
```

| Source | Data Origin | Used By |
|---|---|---|
| `YamlCategorySource` | Local `categories.yaml` | CLI, GUI |
| `PaprikaCategorySource` | Local Paprika SQLite DB | CLI, GUI |
| `SupabaseCategorySource` | Supabase `categories` table | API |

### Design Checkpoints — §2
- [x] `RecipeReader` ABC defined with `read() -> List[Chunk]` signature
- [x] `RecipeWriter` ABC defined with `write(recipes, **kwargs)` signature
- [x] `Chunk` dataclass has all fields: `text`, `source_url`, `image_url`, `image_bytes`, `pre_parsed`, `pre_parsed_embedding`, `input_type`
- [x] `InputType` enum covers: `URL`, `PDF`, `EPUB`, `PAPRIKA_LEGACY`, `PAPRIKA_CAYENNE`
- [ ] All existing readers refactored to return `List[Chunk]` (UrlReader ✓, PaprikaReader ✓; PdfReader and EpubReader pending Phase 3 completion)
- [x] All existing writers refactored to implement `RecipeWriter` (`SupabaseWriter` ✓, `PaprikaWriter` ✓, `CayenneZipWriter` ✓ — Phase 5 complete)

## 3. Core Stage Modules

All stage modules live in `recipeparser/core/stages/`. Each is a **pure function** — no I/O, no global state, no side effects. They accept typed inputs and return typed outputs.

### 3.1 `extract.py`

```python
def extract(chunk_text: str, client) -> List[RecipeExtraction]:
    """
    Call Gemini to extract raw recipe structures from a text chunk.
    Returns an empty list if no recipe is found (not an error).
    Raises ValueError if the chunk is empty.
    """
```

- Wraps existing `gem.extract_recipes()` call
- Handles Baker's % table normalisation (`gem.needs_table_normalisation`)
- Returns `[]` for non-recipe chunks (caller skips silently)

### 3.2 `refine.py`

```python
def refine(
    raw: RecipeExtraction,
    client,
    uom_system: str = 'US',
    measure_preference: str = 'Volume',
) -> RefinedRecipe:
    """
    Convert a raw RecipeExtraction into a Cayenne RefinedRecipe.
    Generates Fat Tokens for directions, structures ingredients,
    and optionally converts Volume → Weight using Gemini density knowledge.
    Raises ValueError if Fat Token generation fails validation.
    """
```

- Wraps existing `gem.refine_recipe_for_cayenne()` call
- Validates Fat Token format: `/\{\{([^|]+)\|([^}]+)\}\}/g`
- Sets `is_ai_converted = True` on ingredients where UOM conversion occurred

### 3.3 `categorize.py`

```python
def categorize(
    recipe: RefinedRecipe,
    client,
    user_axes: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """
    Assign multipolar categories from the user's taxonomy axes.
    Returns {axis_name: [selected_tag, ...]} for each axis.
    Returns {} if user_axes is empty (no categorisation configured).
    """
```

- Wraps existing `cat_module.categorise_recipe()` call
- Gracefully returns `{}` when no axes are configured

### 3.4 `embed.py`

```python
def embed(recipe: RefinedRecipe, client) -> List[float]:
    """
    Generate a 1536-dim embedding vector from the recipe's title + ingredients.
    Uses gemini-embedding-001 model.
    Raises RuntimeError if the API call fails.
    """
```

- Wraps existing `gem.get_embeddings()` call
- Input text: `f"{recipe.title}\n{' '.join(i.fallback_string for i in recipe.structured_ingredients)}"`

### 3.5 `assemble.py`

```python
def assemble(
    recipe: RefinedRecipe,
    embedding: List[float],
    source_url: Optional[str],
    image_url: Optional[str],
    grid_categories: Dict[str, List[str]],
) -> IngestResponse:
    """
    Combine all pipeline outputs into a final IngestResponse.
    Pure function — no API calls.
    """
```

- Constructs `CayenneRecipe` from `RefinedRecipe` fields
- Attaches `embedding`, `source_url`, `image_url`, `grid_categories`
- Returns `IngestResponse` ready for any writer

### Design Checkpoints — §3
- [x] Each stage module has zero imports from `io/` or `adapters/`
- [x] `extract()` returns `[]` (not raises) for non-recipe chunks
- [x] `refine()` raises `ValueError` on Fat Token validation failure (triggers error boundary in pipeline)
- [x] `categorize()` returns `{}` gracefully when `user_axes` is empty
- [x] `embed()` input text format is `"{title}\n{fallback_strings joined by space}"`
- [x] `assemble()` is a pure function with no API calls
- [x] Unit tests exist for each stage with mock `client` objects

## 4. RecipePipeline Orchestrator

`recipeparser/core/pipeline.py` — the single orchestrator for all input types.

### 4.1 Class Interface

```python
class RecipePipeline:
    def __init__(
        self,
        client,
        controller: PipelineController,
        category_source: CategorySource,
        uom_system: str = 'US',
        measure_preference: str = 'Volume',
        concurrency: int = MAX_CONCURRENT_API_CALLS,
        rpm: Optional[int] = None,
    ) -> None: ...

    def run(
        self,
        chunks: List[Chunk],
        on_progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> List[IngestResponse]:
        """
        Process all chunks through the appropriate stage sequence.
        on_progress(stage_name, completed, total) called after each chunk.
        Returns all successfully processed IngestResponse objects.
        Chunks that fail are logged and skipped (never abort the batch).
        """
```

### 4.2 Stage Routing Logic

The pipeline inspects each `Chunk.input_type` to determine which stages to run:

```python
def _get_stages(self, chunk: Chunk) -> List[str]:
    if chunk.input_type == InputType.PAPRIKA_CAYENNE:
        if chunk.pre_parsed_embedding is not None:
            return ['ASSEMBLE']          # $0 — skip all Gemini calls
        return ['EMBED', 'ASSEMBLE']     # Only embed, skip extract/refine/categorize
    # All other types: full pipeline
    return ['EXTRACT', 'REFINE', 'CATEGORIZE', 'EMBED', 'ASSEMBLE']
```

### 4.3 Per-Chunk Error Boundary

```python
for chunk in chunks:
    if not self.controller.check_pause_point():
        break  # cancelled

    try:
        stages = self._get_stages(chunk)
        results = self._process_chunk(chunk, stages)
        all_results.extend(results)
    except Exception as e:
        log.error(f"Chunk failed (skipping): {e}")
        # Never re-raise — continue to next chunk
    finally:
        completed += 1
        if on_progress:
            on_progress(current_stage, completed, total)
```

### 4.4 Parallel Chunk Processing

Chunks are processed in parallel using `ThreadPoolExecutor`. The `GlobalRateLimiter` is acquired inside each worker before any Gemini API call:

```python
with ThreadPoolExecutor(max_workers=self._cap) as executor:
    future_to_chunk = {
        executor.submit(self._process_chunk_safe, chunk): chunk
        for chunk in chunks
    }
    for future in as_completed(future_to_chunk):
        try:
            results = future.result(timeout=SEGMENT_TIMEOUT_SECS)
            all_results.extend(results)
        except TimeoutError:
            log.warning("Chunk timed out — skipping.")
        except Exception as e:
            log.error(f"Chunk worker error: {e}")
```

### 4.5 Checkpoint Integration

After each chunk completes (success or failure), the controller saves a checkpoint:

```python
self.controller.save_checkpoint(
    source_id=source_id,
    completed_chunk_ids=completed_ids,
    partial_results=[r.model_dump() for r in all_results],
)
```

On `run()` start, the controller loads any existing checkpoint and skips already-completed chunks.

### Design Checkpoints — §4
- [x] `RecipePipeline.__init__` accepts `controller`, `category_source`, `uom_system`, `measure_preference`, `concurrency`, `rpm`
- [x] `run()` returns `List[IngestResponse]` (all successful results, not just first)
- [x] `_get_stages()` correctly routes `PAPRIKA_CAYENNE` to `['ASSEMBLE']` or `['EMBED', 'ASSEMBLE']`
- [x] Per-chunk try/except never re-raises — failed chunks are logged and skipped
- [x] `on_progress` callback is called after every chunk (success or failure)
- [ ] Checkpoint is saved after every chunk (deferred to Phase 6 — requires adapter wiring)
- [ ] Checkpoint is loaded on `run()` start; completed chunks are skipped (deferred to Phase 6)
- [x] `ThreadPoolExecutor` is used for parallel chunk processing
- [x] `GlobalRateLimiter.wait_then_record_start()` is called before every Gemini API call

## 5. I/O Writers

### 5.1 `RecipeWriter` ABC (`io/writers/__init__.py`)

```python
class RecipeWriter(ABC):
    @abstractmethod
    def write(self, recipes: List[IngestResponse], **kwargs) -> None: ...
```

### 5.2 `SupabaseWriter` (`io/writers/supabase.py`)

Writes each `IngestResponse` to Supabase via REST API (existing `write_recipe_to_supabase` logic, refactored into the class).

```python
class SupabaseWriter(RecipeWriter):
    def __init__(self, user_id: str, category_ids: Dict[str, str]) -> None: ...
    def write(self, recipes: List[IngestResponse], **kwargs) -> None:
        for recipe in recipes:
            recipe_id = str(uuid.uuid4())
            # POST to /rest/v1/recipes
            # POST to /rest/v1/recipe_categories for each category
```

- Each recipe gets its own UUID
- `recipe_categories` rows are inserted for each matched category
- Image upload is handled by the adapter (before calling `write()`)

### 5.3 `PaprikaWriter` (`io/writers/paprika.py`)

Writes recipes to a `.paprikarecipes` ZIP file (existing `create_paprika_export` logic, refactored).

```python
class PaprikaWriter(RecipeWriter):
    def __init__(self, output_dir: str, filename: str) -> None: ...
    def write(self, recipes: List[IngestResponse], **kwargs) -> None:
        # Flatten structured_ingredients + tokenized_directions to plain text
        # Create ZIP with one gzipped JSON per recipe
```

### 5.4 `CayenneZipWriter` (`io/writers/cayenne_zip.py`)

Writes recipes to a `.cayenne` ZIP file with `_cayenne_meta` embedded (for backup/restore).

```python
class CayenneZipWriter(RecipeWriter):
    def __init__(self, output_dir: str, filename: str) -> None: ...
    def write(self, recipes: List[IngestResponse], **kwargs) -> None:
        # Embed _cayenne_meta (structured_ingredients, tokenized_directions, embedding)
        # Create ZIP compatible with Paprika format (Flow B restore)
```

### Design Checkpoints — §5
- [x] `SupabaseWriter.write()` inserts all recipes (not just first) — iterates `for recipe in recipes` (Phase 5 complete)
- [x] `SupabaseWriter.write()` inserts `recipe_categories` rows for each category match — via `_write_category_junctions()` (Phase 5 complete)
- [x] `PaprikaWriter.write()` produces valid `.paprikarecipes` ZIP — `test_paprika_writer_produces_valid_zip` passes (Phase 5 complete)
- [x] `CayenneZipWriter.write()` embeds `_cayenne_meta` with embedding included — `test_cayenne_zip_writer_embeds_cayenne_meta` passes (Phase 5 complete)
- [x] `CayenneZipWriter` output → `PaprikaReader` → `PAPRIKA_CAYENNE` path → $0 restore — `test_round_trip_cayenne_zip_to_paprika_reader_is_zero_cost` passes (Phase 5 complete)

## 6. GlobalRateLimiter

`recipeparser/core/rate_limiter.py` — process-level singleton enforcing Gemini RPM across all concurrent jobs.

```python
class GlobalRateLimiter:
    """Thread-safe singleton: at most `rpm` Gemini request starts per 60s window."""
    _instance: Optional['GlobalRateLimiter'] = None
    _class_lock = threading.Lock()

    def __new__(cls, rpm: int = 60) -> 'GlobalRateLimiter': ...
    def wait_then_record_start(self) -> None: ...
    def reset(self) -> None: ...  # For testing only
```

- Singleton pattern: all `RecipePipeline` instances share the same limiter
- Workers call `GlobalRateLimiter().wait_then_record_start()` before every Gemini API call
- Blocks until a slot is available in the current 60-second window
- `reset()` is provided for test isolation only

### Design Checkpoints — §6
- [x] `GlobalRateLimiter` is a true singleton (same instance across threads)
- [x] `wait_then_record_start()` is thread-safe (uses `threading.Lock`)
- [x] Multiple concurrent `RecipePipeline` instances share the same limiter
- [ ] Unit test: 10 threads calling `wait_then_record_start()` with `rpm=5` → takes ≥ 60s for all 10 (not yet written)

## 7. FSM Stage Routing

The `PipelineController` FSM (already in `core/fsm.py`) is extended to track the current **stage** within a run, enabling accurate progress reporting and checkpoint labelling.

### 7.1 Extended FSM States

```
IDLE → RUNNING → [LOADING → EXTRACTING → REFINING → CATEGORIZING → EMBEDDING → ASSEMBLING] → DONE
                                                                                    ↑
                                                                          (loops per chunk)
RUNNING → PAUSING → PAUSED → RESUMING → RUNNING
RUNNING/PAUSING/PAUSED → CANCELLING
```

### 7.2 Stage-to-`ingestion_jobs.stage` Mapping

The `stage` column in `ingestion_jobs` must match the DB constraint:

| Pipeline Stage | `ingestion_jobs.stage` value |
|---|---|
| Loading / reading chunks | `LOADING` |
| `extract()` | `EXTRACTING` |
| `refine()` | `REFINING` (new — requires DB migration) |
| `categorize()` | `CATEGORIZING` |
| `embed()` | `EMBEDDING` |
| `assemble()` | `EMBEDDING` (no separate DB stage needed) |
| Complete | `DONE` |
| Error | `ERROR` |

> **Note:** `REFINING` requires adding it to the `ingestion_jobs.stage` CHECK constraint in Supabase. Migration: `007_ingestion_jobs_refining_stage.sql`.

### 7.3 Stage Routing Table

| `Chunk.input_type` | Stages Executed | Gemini Calls |
|---|---|---|
| `URL` | EXTRACT → REFINE → CATEGORIZE → EMBED → ASSEMBLE | 3 |
| `PDF` | EXTRACT → REFINE → CATEGORIZE → EMBED → ASSEMBLE | 3 per chunk |
| `EPUB` | EXTRACT → REFINE → CATEGORIZE → EMBED → ASSEMBLE | 3 per chunk |
| `PAPRIKA_LEGACY` | EXTRACT → REFINE → CATEGORIZE → EMBED → ASSEMBLE | 3 per entry |
| `PAPRIKA_CAYENNE` (no embedding) | EMBED → ASSEMBLE | 1 per entry |
| `PAPRIKA_CAYENNE` (with embedding) | ASSEMBLE | 0 — $0 cost |

### Design Checkpoints — §7
- [ ] `PipelineController` fires `on_stage_change` callback on every stage transition
- [ ] `on_progress` in `RecipePipeline.run()` maps internal stage to `ingestion_jobs.stage` value
- [x] `REFINING` stage added to Supabase `ingestion_jobs.stage` CHECK constraint (migration 007)
- [ ] Stage routing table is unit-tested: each `InputType` produces the correct stage list

## 8. Concurrent Job Handling

### 8.1 Job Isolation

Each `POST /jobs/file` request spawns an independent background task:

| Concern | Isolation Mechanism |
|---|---|
| Data | Supabase RLS (`auth.uid() = user_id`) |
| Progress | Separate `ingestion_jobs` rows (by `job_id`) |
| FSM State | Separate `PipelineController` instances |
| Checkpoints | Separate checkpoint files (by `source_hash`) |
| Rate Limits | Shared `GlobalRateLimiter` singleton |
| Pause/Cancel | Per-job via `PipelineController` |

### 8.2 Job Registry

`api.py` maintains a process-level dict of active controllers:

```python
_active_jobs: Dict[str, PipelineController] = {}
```

- On job start: `_active_jobs[job_id] = controller`
- On job end (done/error/cancel): `del _active_jobs[job_id]`
- `POST /jobs/{job_id}/pause` → `_active_jobs[job_id].request_pause()`
- `POST /jobs/{job_id}/resume` → `_active_jobs[job_id].request_resume()`
- `POST /jobs/{job_id}/cancel` → `_active_jobs[job_id].request_cancel()`

### 8.3 Fair Scheduling

With `GlobalRateLimiter`, concurrent jobs naturally interleave:
- Job A (500-page cookbook) and Job B (50-page cookbook) both call `wait_then_record_start()`
- The limiter serves requests FIFO — no starvation
- Job B completes faster because it has fewer chunks, not because it gets priority

### Design Checkpoints — §8
- [x] `_active_jobs` dict is populated on job start and cleaned up on job end
- [x] `POST /jobs/{job_id}/pause` returns 404 if job not found, 200 if paused
- [x] `POST /jobs/{job_id}/cancel` returns 404 if job not found, 200 if cancelled
- [ ] Two concurrent jobs do not exceed `GlobalRateLimiter` RPM (integration test)
- [ ] Cancelling Job A does not affect Job B

## 9. Build Plan

Each phase ends with a **Gate Test** that must pass before the next phase begins.

---

### Phase 1 — Core Stage Modules
**Goal:** Extract all Gemini logic into pure, testable stage functions.

**Deliverables:**
- `recipeparser/core/stages/__init__.py`
- `recipeparser/core/stages/extract.py`
- `recipeparser/core/stages/refine.py`
- `recipeparser/core/stages/categorize.py`
- `recipeparser/core/stages/embed.py`
- `recipeparser/core/stages/assemble.py`

**Gate Test — Phase 1:**
```bash
pytest tests/unit/stages/ -v
```
- `test_extract_returns_empty_for_non_recipe_text`
- `test_refine_raises_on_invalid_fat_token`
- `test_categorize_returns_empty_dict_when_no_axes`
- `test_embed_input_format_is_title_plus_fallbacks`
- `test_assemble_is_pure_no_api_calls`

All tests use mock `client` objects — zero real API calls.

---

### Phase 2 — GlobalRateLimiter
**Goal:** Implement thread-safe process-level rate limiter.

**Deliverables:**
- `recipeparser/core/rate_limiter.py`

**Gate Test — Phase 2:**
```bash
pytest tests/unit/test_rate_limiter.py -v
```
- `test_singleton_same_instance_across_threads`
- `test_rpm_5_ten_calls_takes_at_least_60s` (use `time.monotonic`)
- `test_reset_clears_state_for_test_isolation`

---

### Phase 3 — Chunk + InputType Models
**Goal:** Define `Chunk` dataclass and `InputType` enum; refactor readers to return `List[Chunk]`.

**Deliverables:**
- `recipeparser/core/models.py` — add `Chunk`, `InputType`
- `recipeparser/io/readers/url.py` — refactored to return `List[Chunk]`
- `recipeparser/io/readers/pdf.py` — refactored to return `List[Chunk]`
- `recipeparser/io/readers/epub.py` — refactored to return `List[Chunk]`
- `recipeparser/io/readers/paprika.py` — refactored; detects `_cayenne_meta` per entry

**Gate Test — Phase 3:**
```bash
pytest tests/unit/readers/ -v
```
- `test_url_reader_returns_single_chunk_with_url_input_type`
- `test_paprika_reader_legacy_entry_returns_paprika_legacy_type`
- `test_paprika_reader_cayenne_entry_with_embedding_returns_cayenne_type`
- `test_paprika_reader_cayenne_entry_without_embedding_returns_cayenne_type_no_embedding`

---

### Phase 4 — RecipePipeline
**Goal:** Implement the orchestrator using stage modules and `GlobalRateLimiter`.

**Deliverables:**
- `recipeparser/core/pipeline.py` — `RecipePipeline` class

**Gate Test — Phase 4:**
```bash
pytest tests/unit/test_pipeline.py -v
```
- `test_pipeline_routes_paprika_cayenne_with_embedding_to_assemble_only`
- `test_pipeline_routes_paprika_cayenne_no_embedding_to_embed_assemble`
- `test_pipeline_routes_url_to_full_pipeline`
- `test_pipeline_skips_failed_chunk_and_continues`
- `test_pipeline_calls_on_progress_after_each_chunk`
- `test_pipeline_respects_cancel_signal`

All tests use mock stage functions — zero real API calls.

---

### Phase 5 — RecipeWriter ABC + Writers
**Goal:** Define `RecipeWriter` ABC and implement all three writers.

**Deliverables:**
- `recipeparser/io/writers/__init__.py` — `RecipeWriter` ABC
- `recipeparser/io/writers/supabase.py` — `SupabaseWriter`
- `recipeparser/io/writers/paprika.py` — `PaprikaWriter`
- `recipeparser/io/writers/cayenne_zip.py` — `CayenneZipWriter`

**Gate Test — Phase 5:**
```bash
pytest tests/unit/writers/ -v
```
- `test_supabase_writer_inserts_all_recipes`
- `test_supabase_writer_inserts_recipe_categories`
- `test_paprika_writer_produces_valid_zip`
- `test_cayenne_zip_writer_embeds_cayenne_meta`
- `test_round_trip_cayenne_zip_to_paprika_reader_is_zero_cost` (Flow B)

---

### Phase 6 — Refactor `api.py`
**Goal:** Replace monolithic `process_epub()` with `RecipePipeline` + `SupabaseWriter`. Introduce the canonical `POST /jobs` and `POST /jobs/file` endpoints that the Cayenne app expects. Deprecate legacy endpoints as shims.

**Deliverables:**
- `recipeparser/adapters/api.py` — refactored with `RecipePipeline` + `SupabaseWriter`
- `recipeparser/adapters/api.py` — `_active_jobs` registry + pause/resume/cancel endpoints
- `recipeparser/adapters/api.py` — `POST /jobs` (text/URL) and `POST /jobs/file` (binary upload) canonical endpoints
- `recipeparser/adapters/api.py` — legacy endpoints (`/ingest`, `/ingest/pdf`, `/ingest/url`) converted to deprecated shims

#### 6.1 Canonical Endpoints (Cayenne App Contract)

The Cayenne mobile app (`ingestionApi.ts`) calls exactly two endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `POST /jobs` | Text/URL submission | Submit a URL or raw text for ingestion |
| `POST /jobs/file` | Binary file upload | Submit a PDF, EPUB, or `.paprikarecipes` file |
| `GET /jobs/{job_id}` | Status poll | Check job status (also synced via PowerSync) |
| `POST /jobs/{job_id}/pause` | Control | Pause a running job |
| `POST /jobs/{job_id}/resume` | Control | Resume a paused job |
| `POST /jobs/{job_id}/cancel` | Control | Cancel a running job |

#### 6.2 Endpoint Deprecation Plan

The legacy endpoints (`/ingest`, `/ingest/pdf`, `/ingest/url`) were the original API surface. They are **not removed in Phase 6** — they become deprecated shims that delegate to the new `POST /jobs` / `POST /jobs/file` handlers internally. This preserves backward compatibility for any non-Cayenne callers (CLI scripts, tests, external integrations) during the transition period.

| Legacy Endpoint | Status in Phase 6 | Removed In |
|---|---|---|
| `POST /ingest` | Deprecated shim → delegates to `POST /jobs` handler | Phase 8 |
| `POST /ingest/pdf` | Deprecated shim → delegates to `POST /jobs/file` handler | Phase 8 |
| `POST /ingest/url` | Deprecated shim → delegates to `POST /jobs` handler | Phase 8 |

Deprecated shims MUST:
1. Log a `DeprecationWarning` on every call: `log.warning("DEPRECATED: /ingest — use POST /jobs instead")`
2. Return the same `202 Accepted` + `{ "job_id": "..." }` response shape as the canonical endpoint
3. Add a `Deprecation: true` response header so callers can detect the deprecation programmatically

#### 6.3 Background Task Concurrency Primitive

`RecipePipeline.run()` is a **synchronous, blocking** function (it uses `ThreadPoolExecutor` internally for chunk parallelism, but the `run()` call itself blocks until all chunks complete). FastAPI is async-native. The adapter MUST offload `run()` to a thread so `POST /jobs` returns `202 Accepted` immediately without blocking the event loop.

**Chosen approach: `asyncio.to_thread()`** (Python 3.9+, FastAPI-native):

```python
# recipeparser/adapters/api.py
import asyncio

@app.post("/jobs", response_model=JobResponse, status_code=202)  # type: ignore[untyped-decorator]
async def submit_job(request: IngestRequest) -> JobResponse:
    job_id = str(uuid.uuid4())
    controller = PipelineController()
    _active_jobs[job_id] = controller

    async def _run_job() -> None:
        try:
            chunks = _build_chunks_from_request(request)
            pipeline = _build_pipeline(controller, request.user_id)
            results = await asyncio.to_thread(
                pipeline.run,
                chunks,
                _strict_observer(_make_progress_callback(job_id, request.user_id)),
            )
            writer = SupabaseWriter(user_id=request.user_id, category_ids=...)
            await asyncio.to_thread(writer.write, results)
        except Exception as e:
            log.error(f"Job {job_id} failed: {e}", exc_info=True)
            _update_job_status(job_id, "error", error_message=str(e))
        finally:
            _active_jobs.pop(job_id, None)

    asyncio.create_task(_run_job())
    return JobResponse(job_id=job_id)
```

**Why `asyncio.to_thread()` over `run_in_executor()`:**
- `asyncio.to_thread()` is the idiomatic Python 3.9+ equivalent of `loop.run_in_executor(None, fn, *args)`
- Both use the default `ThreadPoolExecutor` under the hood — they are functionally identical
- `asyncio.to_thread()` is preferred for new code because it is cleaner, does not require a `loop` reference, and is the documented FastAPI recommendation for sync-in-async offloading

#### 6.4 `POST /jobs/file` Content-Type Routing

`POST /jobs/file` accepts a binary file upload and must route to the correct reader based on the file's content type or filename extension. The routing table:

| `file.content_type` | Filename Extension | Reader |
|---|---|---|
| `application/pdf` | `.pdf` | `PdfReader` |
| `application/epub+zip` | `.epub` | `EpubReader` |
| `application/zip` or `application/octet-stream` | `.paprikarecipes` | `PaprikaReader` |

**Implementation:**

```python
# recipeparser/adapters/api.py
from recipeparser.io.readers.pdf import PdfReader
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.paprika import PaprikaReader

def _select_reader(filename: str, content_type: str) -> RecipeReader:
    """Route to the correct reader based on filename extension (primary) or content-type."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf" or content_type == "application/pdf":
        return PdfReader()
    if ext == ".epub" or content_type == "application/epub+zip":
        return EpubReader()
    if ext == ".paprikarecipes":
        return PaprikaReader()
    raise ValueError(
        f"Unsupported file type: extension='{ext}', content_type='{content_type}'. "
        "Supported: .pdf, .epub, .paprikarecipes"
    )
```

**Routing priority:** Filename extension takes precedence over `content_type` because browsers and HTTP clients frequently misreport MIME types for `.paprikarecipes` files (often sent as `application/octet-stream` or `application/zip`).

**Error response:** Unsupported file types return `422 Unprocessable Entity` with a clear error message — not a 500.

#### 6.5 `_strict_observer` Specificity

The `_strict_observer` wrapper (§11.4) wraps **specifically the `update_ingestion_job()` callback** — the function that writes progress updates to the Supabase `ingestion_jobs` table. This is the only observer that can produce zombie jobs if it fails silently.

```python
# recipeparser/adapters/api.py

def _make_progress_callback(job_id: str, user_id: str) -> Callable[[str, int, int], None]:
    """
    Returns the callback that updates the ingestion_jobs row in Supabase.
    This is the ONLY callback wrapped with _strict_observer — a failure here
    means the job's progress is invisible to the Cayenne app, which is a zombie.
    """
    def update_ingestion_job(stage: str, completed: int, total: int) -> None:
        pct = int((completed / total) * 100) if total > 0 else 0
        _supabase_update_job(job_id=job_id, stage=stage, progress_pct=pct)

    return update_ingestion_job

# In the job handler:
pipeline.run(
    chunks,
    on_progress=_strict_observer(_make_progress_callback(job_id, user_id)),
)
```

**What `_strict_observer` does NOT wrap:** Internal stage callbacks, logging callbacks, or GUI progress callbacks. Only the Supabase `ingestion_jobs` writer is wrapped, because only that callback's failure produces an invisible zombie job.

**Gate Test — Phase 6:**
```bash
pytest tests/test_api.py -v
```
- All existing API tests pass (40/40 ✅ — verified)
- `test_post_jobs_file_returns_202_with_job_id`
- `test_get_jobs_job_id_returns_status`
- `test_post_jobs_pause_returns_200`
- `test_post_jobs_cancel_returns_200`
- `test_post_jobs_cancel_unknown_job_returns_404`
- `test_post_jobs_file_pdf_routes_to_pdf_reader`
- `test_post_jobs_file_epub_routes_to_epub_reader`
- `test_post_jobs_file_paprikarecipes_routes_to_paprika_reader`
- `test_post_jobs_file_unsupported_type_returns_422`
- `test_legacy_ingest_endpoint_returns_deprecation_header`

---

### Phase 7 — DB Migration
**Goal:** Add `REFINING` to `ingestion_jobs.stage` CHECK constraint.

**Deliverables:**
- `cayenne-app/src/db/migrations/007_ingestion_jobs_refining_stage.sql`
- Updated PowerSync sync-rules if needed

**Gate Test — Phase 7:**
```bash
# Apply migration to local Supabase
supabase db push
# Verify constraint
psql -c "INSERT INTO ingestion_jobs (user_id, status, stage) VALUES ('...', 'running', 'REFINING');"
```

---

### Phase 8 — Delete Dead Code
**Goal:** Remove all legacy monolithic functions and deprecated endpoint shims. The codebase must contain only the canonical hexagonal architecture — no legacy bridges, no deprecated routes.

**Targets:**
- `run_cayenne_pipeline()` in `engine.py`
- Old `process_epub()` in `adapters/`
- Duplicate `_RPMRateLimiter` (replaced by `GlobalRateLimiter`)
- Any `process_*` worker functions now handled by stage modules
- **Deprecated endpoint shims:** `POST /ingest`, `POST /ingest/pdf`, `POST /ingest/url` — removed from `adapters/api.py`
- `tests/transient/` directory and `test_shadow_execution.py` (shadow tests no longer needed once legacy code is gone)

**Prerequisite:** `pytest tests/transient/ -v` must pass before this phase begins (§11.2). The shadow execution test proves the new pipeline is output-identical to the legacy path before the legacy path is deleted.

**Gate Test — Phase 8:**
```bash
# Full test suite must still pass after all deletions
pytest tests/ -v

# Legacy function names must be gone
grep -r "run_cayenne_pipeline\|process_epub" recipeparser/
# Must return empty

# Deprecated endpoint routes must be gone
grep -r "app\.post.*\"/ingest\"" recipeparser/adapters/api.py
grep -r "app\.post.*\"/ingest/pdf\"" recipeparser/adapters/api.py
grep -r "app\.post.*\"/ingest/url\"" recipeparser/adapters/api.py
# All three must return empty

# Transient test directory must be deleted
ls tests/transient/ 2>&1 | grep "No such file"
# Must succeed
```

---

### Phase 9 — Integration + E2E Tests
**Goal:** Verify the full pipeline end-to-end with real fixtures.

**Gate Test — Phase 9:**
```bash
pytest tests/integration/ -v -m "not live_api"
```
- `test_multi_recipe_pdf_returns_all_recipes` (mocked Gemini)
- `test_epub_chapter_chunking_produces_correct_chunk_count`
- `test_paprika_legacy_import_calls_full_pipeline`
- `test_paprika_cayenne_import_with_embedding_calls_zero_gemini`
- `test_two_concurrent_jobs_do_not_exceed_rpm`
- `test_cancelled_job_does_not_affect_sibling_job`

---

## 10. Code Reuse Inventory

> **Purpose:** This section is the anti-blanket-rewrite contract. Every function listed here MUST be preserved verbatim (or moved, not rewritten) during the refactor. The refactor is a **structural reorganisation**, not a logic rewrite. If a function is listed as "MOVE", its body must be identical in the new location — only its import path changes.

---

### 10.1 `gemini.py` — Preserve Everything (Move to `core/stages/`)

This file contains hard-won prompt engineering and retry logic. **Nothing in it should be rewritten.**

| Function | Lines of Note | Destination | Action |
|---|---|---|---|
| `_call_with_retry()` | Exponential back-off, 429 detection, `MAX_RETRIES` | `core/stages/_gemini_client.py` | **MOVE** |
| `_is_rate_limit_error()` | Detects `"429"`, `"quota"`, `"resource_exhausted"` | `core/stages/_gemini_client.py` | **MOVE** |
| `verify_connectivity()` | Single-token preflight check | `core/stages/_gemini_client.py` | **MOVE** |
| `get_embeddings()` | `gemini-embedding-001`, 1536-dim, raises on failure | `core/stages/embed.py` | **MOVE** (wrap in `embed()`) |
| `needs_table_normalisation()` | Baker's % regex, handles Unicode U+FFFD | `core/stages/extract.py` | **MOVE** (called inside `extract()`) |
| `normalise_baker_table()` | Pre-processing prompt, falls back to original on failure | `core/stages/extract.py` | **MOVE** (called inside `extract()`) |
| `extract_recipes()` | Full extraction prompt with hero image, phase, UOM rules | `core/stages/extract.py` | **MOVE** (called inside `extract()`) |
| `extract_recipe_from_text()` | Simpler prompt for plain-text (Paprika legacy) | `core/stages/extract.py` | **MOVE** (called inside `extract()`) |
| `extract_text_via_vision()` | OCR fallback for scanned PDFs, 2× scale pixmap | `core/stages/extract.py` | **MOVE** (called inside `extract()`) |
| `refine_recipe_for_cayenne()` | Fat Token generation + UOM conversion + categorization in one call | `core/stages/refine.py` | **MOVE** (called inside `refine()`) |
| `_build_dynamic_grid_schema()` | Runtime Pydantic model for multipolar categorization | `core/stages/refine.py` | **MOVE** (called inside `refine()`) |
| `_format_axes_for_prompt()` | Formats user axes into prompt section | `core/stages/refine.py` | **MOVE** (called inside `refine()`) |
| `_UNITS_RULES` | Dict of UOM prompt rules (metric/us/imperial/book) | `core/stages/extract.py` | **MOVE** |

> **Critical:** The `refine_recipe_for_cayenne()` function combines Fat Token generation, UOM conversion, AND categorization in a single Gemini call. This is intentional — splitting them into separate API calls would triple the cost. The `categorize()` stage module wraps the `grid_categories` extraction that already happens inside `refine()`, not a separate API call.

---

### 10.2 `pipeline.py` — Selective Reuse

| Function / Class | Lines of Note | Destination | Action |
|---|---|---|---|
| `_RPMRateLimiter` | Thread-safe sliding window, `wait_then_record_start()` | `core/rate_limiter.py` as `GlobalRateLimiter` | **MOVE + PROMOTE** to singleton |
| `_process_segment()` | Semaphore + rate limiter + Baker's % + extract call | `core/pipeline.py` (inlined into `RecipePipeline._process_chunk_safe()`) | **ABSORB** |
| `PipelineContext` | Bundles shared state for workers | Replaced by `RecipePipeline` instance attributes | **DELETE** (superseded) |
| Checkpoint load/save logic | `controller.load_checkpoint()` / `controller.save_checkpoint()` | `core/pipeline.py` | **MOVE** verbatim |
| Hero-image look-ahead injection | `_IMAGE_ONLY_RE` regex + prepend logic | `io/readers/epub.py` (inside `EpubReader.read()`) | **MOVE** to reader |
| `candidate_chunks` filter | `is_recipe_candidate()` filter before thread pool | `io/readers/epub.py` (inside `EpubReader.read()`) | **MOVE** to reader |
| Deduplication call | `deduplicate_recipes(all_recipes)` | `core/pipeline.py` (after all chunks processed) | **KEEP** |
| Recon call | `run_recon(toc_entries, extracted_names)` | `core/pipeline.py` (after dedup) | **KEEP** |
| Run summary logging | `log.info("--- Run summary ---")` block | `core/pipeline.py` | **KEEP** |
| `Stage`, `ChunkingPath`, `ReconStatus`, `PreflightOutcome` enums | Pipeline state tracking | `core/pipeline.py` | **KEEP** |
| `PipelineState` dataclass | Stage tracking for GUI/logging | `core/pipeline.py` | **KEEP** |

---

### 10.3 `io/readers/epub.py` — Keep Entirely

This file is already well-structured. **No logic changes needed** — only the `load_epub()` return signature changes to return `List[Chunk]` instead of a tuple.

| Function | Lines of Note | Action |
|---|---|---|
| `load_epub()` | Opens EPUB, extracts images, returns chunks | **ADAPT** return type to `List[Chunk]` |
| `extract_all_images()` | `MIN_PHOTO_BYTES` filter, saves to `images/` dir | **KEEP** verbatim |
| `extract_chapters_with_image_markers()` | `[IMAGE: filename]` breadcrumb injection | **KEEP** verbatim |
| `split_large_chunk()` | Paragraph-boundary splitting at `MAX_CHUNK_CHARS` | **KEEP** verbatim |
| `is_recipe_candidate()` | Quantity + structure keyword heuristic | **KEEP** verbatim |
| `get_book_source()` | DC metadata extraction, `Title — Author` format | **KEEP** verbatim |
| `extract_text_from_epub()` | Stateless text extraction (used by Paprika legacy path) | **KEEP** verbatim |

---

### 10.4 `io/readers/pdf.py` — Keep Entirely

Same pattern as EPUB reader. Adapt return type only.

| Function | Lines of Note | Action |
|---|---|---|
| `load_pdf()` | Text extraction + OCR fallback detection | **ADAPT** return type to `List[Chunk]` |
| OCR fallback detection | Checks if extracted text is too sparse → triggers `extract_text_via_vision()` | **KEEP** verbatim |

---

### 10.5 `io/readers/paprika.py` — Extend, Don't Rewrite

| Function | Lines of Note | Action |
|---|---|---|
| Existing ZIP parsing | Reads `.paprikarecipes` ZIP, decompresses gzipped JSON entries | **KEEP** verbatim |
| `_cayenne_meta` detection | Checks for `_cayenne_meta` key in each entry | **KEEP** verbatim |
| New: `input_type` assignment | Set `InputType.PAPRIKA_CAYENNE` or `InputType.PAPRIKA_LEGACY` per entry | **ADD** |
| New: `pre_parsed` population | Populate `Chunk.pre_parsed` from `_cayenne_meta` when present | **ADD** |

---

### 10.6 `core/fsm.py` — Keep Entirely

`PipelineController` is already well-designed. Only additions needed:

| Addition | Notes |
|---|---|
| `on_stage_change` callback | Fire on every stage transition for progress reporting |
| `request_pause()` / `request_resume()` / `request_cancel()` | Public API for `api.py` job registry (may already exist) |

---

### 10.7 `core/engine.py` — Keep Entirely

| Function | Notes | Action |
|---|---|---|
| `deduplicate_recipes()` | Title-normalised dedup | **KEEP** verbatim |
| `run_cayenne_pipeline()` | Legacy shim — delete in Phase 8 | **DELETE** in Phase 8 |

---

### 10.8 `io/writers/supabase.py` — Refactor to Class

The existing `write_recipe_to_supabase()` function contains all the correct Supabase REST logic. Wrap it in `SupabaseWriter` class — do not rewrite the HTTP calls.

---

### 10.9 `io/writers/paprika_zip.py` — Refactor to Class

The existing `create_paprika_export()` function contains the correct ZIP/gzip format. Wrap it in `PaprikaWriter` class — do not rewrite the ZIP logic.

> **Phase 5 Implementation Note — Justified Deviation:** The spec mandated wrapping `create_paprika_export()` directly. This was not possible because `create_paprika_export()` takes `List[RecipeExtraction]` (the legacy model with `photo_filename`, `directions: List[str]`, `name`, etc.) while `PaprikaWriter.write()` takes `List[IngestResponse]` (the new model with `structured_ingredients`, `tokenized_directions`, `title`, etc.). The types are structurally incompatible — a direct call would require converting `IngestResponse` back to `RecipeExtraction`, which would be a lossy round-trip. Instead, `PaprikaWriter` uses a new helper `_ingest_to_paprika_dict()` that produces the identical Paprika 3 JSON dict format using the new model's fields. The ZIP/gzip format, field names, and output structure are identical to `create_paprika_export()` — only the input model changed. `create_paprika_export()` is preserved verbatim for legacy `RecipeExtraction` callers (CLI/GUI adapters not yet migrated to Phase 6).

---

### 10.10 What Is Actually New (Not Reused)

| New Component | Why It's New |
|---|---|
| `GlobalRateLimiter` singleton | Promotes `_RPMRateLimiter` to process-level singleton |
| `Chunk` dataclass + `InputType` enum | New abstraction for unified reader output |
| `RecipePipeline` class | New orchestrator replacing `process_epub()` monolith |
| `RecipeWriter` ABC | New abstraction for pluggable writers |
| `CayenneZipWriter` | New writer for backup/restore format |
| `core/stages/__init__.py` | New package |
| Stage function wrappers (`extract()`, `refine()`, etc.) | Thin wrappers around existing `gemini.py` functions |
| `_active_jobs` registry in `api.py` | New for pause/resume/cancel endpoints |

> **Rule:** If a component is not in the "What Is Actually New" table above, it must be moved or adapted — never rewritten from scratch.

---

## 11. Refactor Safety Controls

These controls are mandatory additions to the refactor. They are not optional enhancements — they are the mechanical enforcement layer that makes the architectural guarantees in Sections 1–10 verifiable and regression-proof.

---

### 11.1 Automated Boundary Enforcement (Import Linting)

**Purpose:** Mechanically enforce Design Checkpoint §1. Prevents developers from accidentally pulling I/O logic back into the pure core during complex refactoring phases. A CI failure is cheaper than a code review miss.

**Implementation:** Configure `ruff` with `flake8-tidy-imports` (TID rule set) in `pyproject.toml`:

```toml
[tool.ruff]
line-length = 120
target-version = "py39"

[tool.ruff.lint]
select = ["E", "F", "I", "TID"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"recipeparser.io".msg = "core/ modules cannot import from io/ — violates hexagonal architecture"
"recipeparser.adapters".msg = "core/ modules cannot import from adapters/ — violates hexagonal architecture"

[tool.ruff.lint.per-file-ignores]
"recipeparser/io/**" = ["TID"]
"recipeparser/adapters/**" = ["TID"]
```

**Gate Test:**
```bash
ruff check recipeparser/core/ --select TID
# Must return zero violations — CI fails on any boundary breach
```

**Phase:** Configured in **Phase 0** before any file moves. The gate test is added to CI as a required check.

### Design Checkpoints — §11.1
- [x] `[tool.ruff]` and `[tool.ruff.lint.flake8-tidy-imports.banned-api]` configured in `pyproject.toml`
- [x] `ruff check recipeparser/core/ --select TID` returns zero violations after Phase 1

---

### 11.2 Shadow Execution (Dual-Run Tests)

**Purpose:** Guarantee the structural refactor didn't accidentally drop fields, alter unit conversions, or skip Fat Token generation. The LLM logic in `gemini.py` is being moved without modification — this test proves it.

**Implementation:** Create `tests/transient/test_shadow_execution.py`. Feed identical complex recipe text to both the legacy path and the new `RecipePipeline` using `MockProvider`. Assert that the resulting `CayenneRecipe` Pydantic models dump to identical dictionaries.

```python
# tests/transient/test_shadow_execution.py
# TRANSIENT: Delete this file in Phase 8 when legacy code is removed.
import pytest
from recipeparser.pipeline import process_epub  # Legacy
from recipeparser.core.pipeline import RecipePipeline  # New
from recipeparser.core.models import Chunk, InputType

COMPLEX_RECIPE_TEXT = "..."  # Multi-ingredient recipe with Baker's %, UOM conversions

@pytest.mark.transient
def test_shadow_execution_produces_identical_output():
    """Structural refactor must not alter any field in the output model."""
    legacy_result = process_epub(COMPLEX_RECIPE_TEXT, mock=True)
    new_result = RecipePipeline(mock=True).run(
        [Chunk(text=COMPLEX_RECIPE_TEXT, input_type=InputType.EPUB)]
    )
    assert legacy_result[0].model_dump() == new_result[0].model_dump(), \
        "Refactor introduced data shape divergence — FAIL LOUDLY"
```

**Gate Test:** Must pass before Phase 8 (Delete Dead Code) begins. The `tests/transient/` directory and this file are deleted as part of the Phase 8 deliverables.

**Phase:** Written in **Phase 6** (alongside refactored `api.py`); deleted in **Phase 8**.

### Design Checkpoints — §11.2
- [x] `tests/transient/test_shadow_execution.py` created in Phase 6
- [x] `pytest tests/transient/ -v` passes before Phase 8 gate
- [x] `tests/transient/` directory deleted in Phase 8 deliverables
- [x] Phase 8 gate test includes `ls tests/transient/ 2>&1 | grep "No such file"` to confirm deletion

---

### 11.3 Snapshot Testing for Extracted Formats

**Purpose:** The hard-won LLM extraction logic (`_UNITS_RULES`, table normalisation, Fat Token generation) is the most valuable asset in the repository. Snapshot tests instantly flag if the shape of data returned by the LLM changes due to how input text chunks are formatted or passed in — catching regressions introduced during the stage-wrapping refactor.

**Implementation:** Add `pytest-syrupy` to dev dependencies. Create snapshot tests for each stage's output shape.

```toml
# pyproject.toml
[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "syrupy>=4.0",
    "ruff>=0.4",
    "mypy>=1.10",
]
```

```python
# tests/snapshots/test_stage_snapshots.py
from syrupy.assertion import SnapshotAssertion
from recipeparser.core.stages.extract import extract
from recipeparser.core.stages.refine import refine

def test_extract_stage_output_shape(snapshot: SnapshotAssertion, mock_client):
    """Snapshot the full model_dump() of extract() output."""
    result = extract(FIXTURE_CHUNK_TEXT, mock_client)
    assert result[0].model_dump() == snapshot

def test_refine_stage_output_shape(snapshot: SnapshotAssertion, mock_client):
    """Snapshot the full model_dump() of refine() output including Fat Tokens."""
    result = refine(FIXTURE_EXTRACTION, mock_client)
    assert result.model_dump() == snapshot
```

**Gate Test:** Snapshot tests run in CI. Any shape change requires explicit `--snapshot-update` with a mandatory code review comment explaining the intentional change.

**Phase:** Created in **Phase 1** alongside the stage modules. Snapshots are committed to the repository.

### Design Checkpoints — §11.3
- [x] `pytest-syrupy>=4.0` added to `[project.optional-dependencies.dev]` in `pyproject.toml`
- [x] `tests/snapshots/test_stage_snapshots.py` created in Phase 1
- [x] Snapshot files committed to repository (not gitignored)
- [x] `pytest tests/snapshots/ -v` passes in CI without `--snapshot-update`

---

### 11.4 Strict Type-Checking on FSM Observers (Fail Early, Fail Loudly)

**Purpose:** A silently failing observer in `api.py` could result in "zombie" ingestion jobs in the Cayenne app that never transition from `RUNNING` to `DONE`. The fix is not to swallow errors — it is to **crash the job loudly** so the failure is immediately visible in logs and the job transitions to `ERROR` state.

**Implementation:**

**Step 1 — Explicit `Protocol` typing in `core/fsm.py`:**

```python
# recipeparser/core/fsm.py
from typing import Callable, Optional, Protocol

class ProgressCallback(Protocol):
    """Strict type contract for pipeline progress observers."""
    def __call__(self, stage: str, completed: int, total: int) -> None: ...

class PipelineController:
    def __init__(
        self,
        on_progress: Optional[ProgressCallback] = None,
        on_stage_change: Optional[Callable[[str], None]] = None,
    ) -> None: ...
```

**Step 2 — Fail-loudly wrapper in `adapters/api.py` (log + re-raise):**

```python
# recipeparser/adapters/api.py
import logging
from typing import Callable

log = logging.getLogger(__name__)

def _strict_observer(callback: Callable[[str, int, int], None]) -> Callable[[str, int, int], None]:
    """
    Wrap observer callbacks with strict error handling.
    Logs the error and RE-RAISES — crashing the job so it transitions to ERROR,
    not silently continuing as a zombie.
    """
    def wrapper(stage: str, completed: int, total: int) -> None:
        try:
            callback(stage, completed, total)
        except Exception as e:
            log.error(f"Observer callback FAILED (job will be marked ERROR): {e}", exc_info=True)
            raise  # FAIL LOUDLY — no zombies
    return wrapper

# Usage in job handler:
pipeline.run(chunks, on_progress=_strict_observer(update_job_progress))
```

**Step 3 — mypy strict mode:**

```toml
# pyproject.toml
[tool.mypy]
python_version = "3.9"
strict = true
warn_return_any = true
warn_unused_ignores = true

[[tool.mypy.overrides]]
module = "recipeparser.core.fsm"
disallow_untyped_defs = true
disallow_any_generics = true
```

**Gate Test:**
```bash
mypy recipeparser/core/fsm.py recipeparser/adapters/api.py --strict
# Must return zero errors
```

**Phase:** `ProgressCallback` Protocol and mypy config added in **Phase 1**. `_strict_observer` wrapper added in **Phase 6** alongside the refactored `api.py`.

### Design Checkpoints — §11.4
- [x] `ProgressCallback` Protocol defined in `core/fsm.py`
- [x] `PipelineController.__init__` typed with `Optional[ProgressCallback]`
- [x] `[tool.mypy]` configured in `pyproject.toml` with `strict = true`
- [x] `notify_progress()` re-raises observer exceptions after `log.exception()` — no zombie jobs (commit 2a8b58f)
- [x] Unit test: `test_notify_progress_raises_when_callback_fails` asserts `RuntimeError` propagates — 41/41 pass
- [x] `_strict_observer` wrapper implemented in `adapters/api.py` — re-raises on failure (Phase 6)
- [x] `mypy recipeparser/core/fsm.py recipeparser/adapters/api.py --strict` returns zero errors (Phase 6)

---

## Phase 0 — Tooling Setup

**Insert before Phase 1.**

**Goal:** Configure all static analysis and testing tooling before any code changes. This ensures boundary violations are caught from the first file move, not discovered after the refactor is complete.

**Deliverables:**
- Updated `pyproject.toml` with `[tool.ruff]`, `[tool.mypy]`, and `[project.optional-dependencies.dev]`
- `tests/snapshots/` directory (with `.gitkeep`)
- `tests/transient/` directory (with `.gitkeep`)

**Gate Test — Phase 0:**
```bash
# Install dev dependencies
pip install -e ".[dev]"

# Baseline boundary check (violations expected at this point — document them)
ruff check recipeparser/core/ --select TID 2>&1 | tee baseline_violations.txt

# Baseline mypy (errors expected — document them)
mypy recipeparser/ --strict 2>&1 | tee baseline_mypy.txt

# Confirm syrupy is available
python -c "import syrupy; print('syrupy OK')"
```

The baseline violation counts are recorded. After Phase 1, `ruff check recipeparser/core/ --select TID` must return zero.

---

### Definition of Done (All Phases)

- [x] `pytest tests/ -v` — all tests pass
- [ ] `mypy recipeparser/` — zero type errors
- [ ] `ruff check recipeparser/` — zero lint errors
- [ ] `ruff check recipeparser/core/ --select TID` — zero boundary violations (§11.1)
- [x] `pytest tests/snapshots/ -v` — all snapshot tests pass without `--snapshot-update` (§11.3)
- [ ] `mypy recipeparser/core/fsm.py recipeparser/adapters/api.py --strict` — zero type errors (§11.4)
- [x] `grep -r "from recipeparser.io\|from recipeparser.adapters" recipeparser/core/` — returns empty (no layer violations)
- [x] `PIPELINE_REFACTOR.md` design checkpoints all ticked
