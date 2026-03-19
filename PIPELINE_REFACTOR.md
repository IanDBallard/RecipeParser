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
- [ ] All `core/` modules import only from `core/` or stdlib (no `io/`, no `adapters/`)
- [ ] All `io/` modules import only from `core/` or stdlib (no `adapters/`)
- [ ] All `adapters/` modules import from `io/` and `core/` only
- [ ] No circular imports between layers (verified with `pydeps` or `importlib`)

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
- [ ] `RecipeReader` ABC defined with `read() -> List[Chunk]` signature
- [ ] `RecipeWriter` ABC defined with `write(recipes, **kwargs)` signature
- [ ] `Chunk` dataclass has all fields: `text`, `source_url`, `image_url`, `image_bytes`, `pre_parsed`, `pre_parsed_embedding`, `input_type`
- [ ] `InputType` enum covers: `URL`, `PDF`, `EPUB`, `PAPRIKA_LEGACY`, `PAPRIKA_CAYENNE`
- [ ] All existing readers refactored to return `List[Chunk]`
- [ ] All existing writers refactored to implement `RecipeWriter`

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
- [ ] Each stage module has zero imports from `io/` or `adapters/`
- [ ] `extract()` returns `[]` (not raises) for non-recipe chunks
- [ ] `refine()` raises `ValueError` on Fat Token validation failure (triggers error boundary in pipeline)
- [ ] `categorize()` returns `{}` gracefully when `user_axes` is empty
- [ ] `embed()` input text format is `"{title}\n{fallback_strings joined by space}"`
- [ ] `assemble()` is a pure function with no API calls
- [ ] Unit tests exist for each stage with mock `client` objects

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
- [ ] `RecipePipeline.__init__` accepts `controller`, `category_source`, `uom_system`, `measure_preference`, `concurrency`, `rpm`
- [ ] `run()` returns `List[IngestResponse]` (all successful results, not just first)
- [ ] `_get_stages()` correctly routes `PAPRIKA_CAYENNE` to `['ASSEMBLE']` or `['EMBED', 'ASSEMBLE']`
- [ ] Per-chunk try/except never re-raises — failed chunks are logged and skipped
- [ ] `on_progress` callback is called after every chunk (success or failure)
- [ ] Checkpoint is saved after every chunk
- [ ] Checkpoint is loaded on `run()` start; completed chunks are skipped
- [ ] `ThreadPoolExecutor` is used for parallel chunk processing
- [ ] `GlobalRateLimiter.wait_then_record_start()` is called before every Gemini API call

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
- [ ] `SupabaseWriter.write()` inserts all recipes (not just first)
- [ ] `SupabaseWriter.write()` inserts `recipe_categories` rows for each category match
- [ ] `PaprikaWriter.write()` produces valid `.paprikarecipes` ZIP (round-trip test passes)
- [ ] `CayenneZipWriter.write()` embeds `_cayenne_meta` with embedding included
- [ ] `CayenneZipWriter` output → `PaprikaReader` → `PAPRIKA_CAYENNE` path → $0 restore (round-trip test)

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
- [ ] `GlobalRateLimiter` is a true singleton (same instance across threads)
- [ ] `wait_then_record_start()` is thread-safe (uses `threading.Lock`)
- [ ] Multiple concurrent `RecipePipeline` instances share the same limiter
- [ ] Unit test: 10 threads calling `wait_then_record_start()` with `rpm=5` → takes ≥ 60s for all 10

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
- [ ] `REFINING` stage added to Supabase `ingestion_jobs.stage` CHECK constraint (migration 007)
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
- [ ] `_active_jobs` dict is populated on job start and cleaned up on job end
- [ ] `POST /jobs/{job_id}/pause` returns 404 if job not found, 200 if paused
- [ ] `POST /jobs/{job_id}/cancel` returns 404 if job not found, 200 if cancelled
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
**Goal:** Replace monolithic `process_epub()` with `RecipePipeline` + `SupabaseWriter`.

**Deliverables:**
- `recipeparser/adapters/api.py` — refactored
- `recipeparser/adapters/api.py` — `_active_jobs` registry + pause/resume/cancel endpoints

**Gate Test — Phase 6:**
```bash
pytest tests/test_api.py -v
```
- All existing API tests pass
- `test_post_jobs_file_returns_202_with_job_id`
- `test_get_jobs_job_id_returns_status`
- `test_post_jobs_pause_returns_200`
- `test_post_jobs_cancel_returns_200`
- `test_post_jobs_cancel_unknown_job_returns_404`

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
**Goal:** Remove all legacy monolithic functions.

**Targets:**
- `run_cayenne_pipeline()` in `engine.py`
- Old `process_epub()` in `adapters/`
- Duplicate `_RPMRateLimiter` (replaced by `GlobalRateLimiter`)
- Any `process_*` worker functions now handled by stage modules

**Gate Test — Phase 8:**
```bash
pytest tests/ -v  # Full suite — all tests still pass
grep -r "run_cayenne_pipeline\|process_epub" recipeparser/ # Must return empty
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

### Definition of Done (All Phases)

- [ ] `pytest tests/ -v` — all tests pass
- [ ] `mypy recipeparser/` — zero type errors
- [ ] `ruff check recipeparser/` — zero lint errors
- [ ] `grep -r "from recipeparser.io\|from recipeparser.adapters" recipeparser/core/` — returns empty (no layer violations)
- [ ] `PIPELINE_REFACTOR.md` design checkpoints all ticked
