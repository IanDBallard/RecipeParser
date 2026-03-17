# RecipeParser Architecture & Design

## 1. Overview & Goals

RecipeParser is an AI-powered recipe extraction engine that converts any recipe source (EPUB, PDF, URL, plain text, or Paprika archives) into structured `CayenneRecipe` objects with Fat Token directions, scaled ingredients, and semantic embeddings.

### Design Goals

- **Zero technical debt** — no compatibility shims, no legacy bridges, no "temporary" workarounds.
- **Pure core engine** — the extraction engine has no knowledge of file I/O, network, databases, or UI. It accepts text and returns data.
- **Pluggable AI providers** — LLM and embedding backends are swappable via a Protocol interface. Adding a new provider requires only a new file.
- **Wrapper-owned concerns** — file I/O, category sourcing, output format, and status reporting are the responsibility of the adapter (CLI, GUI, or API), not the engine.
- **Externalized FSM** — pipeline state is a first-class object, observable by any adapter and (via Supabase + PowerSync) by the mobile app in real time.
- **Unified image storage** — all recipe images are stored in Supabase Storage regardless of ingestion path. ZIP outputs reference URLs; Paprika-compat ZIPs also embed bytes.

## 2. Module Map

```
recipeparser/
│
├── core/                          # Pure extraction engine — no I/O, no side effects
│   ├── engine.py                  # RecipeEngine class — orchestrates the pipeline
│   ├── chunker.py                 # Splits source text into processable segments
│   ├── fsm.py                     # ExtractionFSM — externalized state machine
│   └── providers/
│       ├── base.py                # LLMProvider + EmbeddingProvider ABCs
│       ├── factory.py             # create_provider() / create_embedding_provider()
│       ├── gemini.py              # GeminiProvider (extraction, refinement, categorization) + GeminiEmbeddingProvider (gemini-embedding-001)
│       ├── openai.py              # OpenAIProvider (GPT-4o extraction — future)
│       ├── anthropic.py           # AnthropicProvider (Claude — future)
│       └── mock.py                # MockProvider + MockEmbeddingProvider (tests)
│
├── io/
│   ├── readers/                   # Source → SourceDocument(text, images)
│   │   ├── base.py                # SourceReader ABC + SourceDocument dataclass
│   │   ├── epub.py                # EPUB reader
│   │   ├── pdf.py                 # PDF reader
│   │   ├── url.py                 # URL reader (via Jina r.jina.ai)
│   │   ├── text.py                # Plain text passthrough
│   │   └── paprika.py             # .paprikarecipes reader (Paprika + Cayenne formats)
│   ├── writers/                   # List[CayenneRecipe] + images → output file
│   │   ├── base.py                # RecipeWriter ABC
│   │   ├── cayenne_zip.py         # .cayennerecipes ZIP (Cayenne JSON + image URLs)
│   │   └── paprika_zip.py         # .paprikarecipes ZIP (Paprika JSON + embedded images)
│   └── category_sources/          # Taxonomy → CategoryTree
│       ├── base.py                # CategorySource ABC + CategoryTree dataclass
│       ├── yaml_source.py         # Load from categories.yaml
│       ├── paprika_db_source.py   # Load from Paprika SQLite
│       └── supabase_source.py     # Load from Supabase categories table
│
├── adapters/                      # Environment-specific wrappers
│   ├── cli.py                     # CLI entry point (replaces __main__.py)
│   ├── gui.py                     # GUI wrapper (replaces gui.py)
│   └── api.py                     # FastAPI wrapper (replaces api.py)
│
├── models.py                      # Pydantic models (source of truth for all data shapes)
├── config.py                      # Constants (retry limits, backoff, concurrency caps)
├── exceptions.py                  # RecipeParserError hierarchy
└── __main__.py                    # Entry point: from recipeparser.adapters.cli import main; main()
```

## 3. Core Engine

The `RecipeEngine` is the heart of the system. It is a pure Python class with no imports from `io/` or `adapters/`. All external dependencies are injected.

### Key Data Types

```python
# core/engine.py

@dataclass
class EngineConfig:
    units: str = "book"                  # "metric" | "us" | "imperial" | "book"
    uom_system: str = "US"               # "US" | "Metric" | "Imperial"
    measure_preference: str = "Volume"   # "Volume" | "Weight"
    concurrency: int = 1                 # max parallel Gemini calls
    rpm: Optional[int] = None            # requests-per-minute cap (None = unlimited)

@dataclass
class ImageAsset:
    filename: str                        # original filename from source
    data: bytes                          # raw image bytes
    mime_type: str                       # e.g. "image/jpeg"

@dataclass
class SourceDocument:
    text: str                            # extracted plain text
    images: List[ImageAsset]             # images extracted from source
    source_url: Optional[str] = None     # original URL if applicable

@dataclass
class ExtractionResult:
    recipes: List[CayenneRecipe]         # fully structured recipes
    embeddings: List[List[float]]        # parallel list — one 1536-dim vector per recipe
    images: List[ImageAsset]             # pass-through from SourceDocument
    stats: dict                          # {"chunks": int, "raw_extracted": int, "refined": int}
```

### RecipeEngine Contract

```python
class RecipeEngine:
    def __init__(
        self,
        llm: LLMProvider,
        embedder: EmbeddingProvider,
        config: EngineConfig,
        fsm: Optional[ExtractionFSM] = None,   # injected for observability
    ): ...

    def extract(
        self,
        doc: SourceDocument,
        category_tree: CategoryTree,
    ) -> ExtractionResult:
        """
        Full pipeline: chunk → extract → deduplicate → categorize → refine → embed.
        FSM transitions are fired at each stage boundary.
        Raises RecipeParserError on unrecoverable failure.
        """
```

### Pipeline Sequence

```
SourceDocument.text
       │
       ▼
  [CHUNKING]  chunker.py → List[str]
       │
       ▼
  [EXTRACTING]  llm.extract_recipes(chunk) → List[RecipeExtraction]  (per chunk, concurrent)
       │
       ▼
  [DEDUP]  normalize names, remove duplicates
       │
       ▼
  [CATEGORIZING]  llm.categorize(recipe, category_tree) → List[str]  (per recipe)
       │
       ▼
  [REFINING]  llm.refine_recipe(raw) → CayenneRefinement  (per recipe)
       │
       ▼
  [EMBEDDING]  embedder.embed(title + ingredients) → List[float]  (per recipe)
       │
       ▼
  ExtractionResult
```

## 4. Externalized FSM

The `ExtractionFSM` in `core/fsm.py` is a pure state machine. It holds the current state and fires observer callbacks on every transition. It has no knowledge of Supabase, files, or UI.

### States

```
IDLE → LOADING → CHUNKING → EXTRACTING → CATEGORIZING → REFINING → EMBEDDING → DONE
                                                                              ↘ ERROR
```

| State | Description |
|-------|-------------|
| `IDLE` | Initial state. Engine instantiated but not started. |
| `LOADING` | Source document is being read by the adapter (reader). |
| `CHUNKING` | Text is being split into processable segments. |
| `EXTRACTING` | Gemini is extracting raw recipes from chunks. |
| `CATEGORIZING` | Gemini is assigning taxonomy categories to each recipe. |
| `REFINING` | Gemini is converting raw recipes to Fat Token / Cayenne format. |
| `EMBEDDING` | Gemini `gemini-embedding-001` is generating 1536-dim vectors for each recipe. |
| `DONE` | All recipes extracted, refined, and embedded successfully. |
| `ERROR` | Unrecoverable failure. `error_message` is set. |

### FSM Interface

```python
# core/fsm.py
from enum import Enum, auto
from typing import Callable, Optional

class ExtractionState(Enum):
    IDLE        = auto()
    LOADING     = auto()
    CHUNKING    = auto()
    EXTRACTING  = auto()
    CATEGORIZING = auto()
    REFINING    = auto()
    EMBEDDING   = auto()
    DONE        = auto()
    ERROR       = auto()

class ExtractionFSM:
    def __init__(self):
        self.state: ExtractionState = ExtractionState.IDLE
        self.progress: int = 0          # 0–100
        self.recipe_count: int = 0      # recipes found so far
        self.error_message: Optional[str] = None
        self._observers: list[Callable[["ExtractionFSM"], None]] = []

    def add_observer(self, fn: Callable[["ExtractionFSM"], None]) -> None:
        """Register a callback invoked on every state transition."""
        self._observers.append(fn)

    def transition(
        self,
        new_state: ExtractionState,
        progress: int = 0,
        recipe_count: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """Advance to new_state and notify all observers."""
        self.state = new_state
        self.progress = progress
        self.recipe_count = recipe_count
        self.error_message = error
        for fn in self._observers:
            fn(self)
```

### Observer Pattern — Adapter Responsibility

Each adapter registers its own observer(s):

| Adapter | Observer Action |
|---------|----------------|
| CLI | `log.info("State: %s (%d%%)", fsm.state.name, fsm.progress)` |
| GUI | Update progress bar + status label in the UI thread |
| API | `UPDATE ingestion_jobs SET stage=..., progress_pct=..., status=... WHERE id=:job_id` |

The engine calls `fsm.transition(...)` at each stage boundary. The adapter decides what to do with that information.

## 5. LLM Provider Interface

All LLM operations are accessed through the `LLMProvider` ABC defined in `core/providers/base.py`. The engine imports only this interface — never a concrete provider.

```python
# core/providers/base.py
from abc import ABC, abstractmethod
from typing import List, Optional
from recipeparser.models import RecipeList, CayenneRefinement, RecipeExtraction

class LLMProvider(ABC):
    """Abstract interface for recipe extraction, refinement, and categorization."""

    @abstractmethod
    def verify_connectivity(self) -> bool:
        """Confirm the provider API is reachable. Called once before processing."""
        ...

    @abstractmethod
    def extract_recipes(self, text: str, units: str) -> Optional[RecipeList]:
        """Extract raw recipes from a text chunk. Returns None on failure."""
        ...

    @abstractmethod
    def refine_recipe(
        self,
        raw: RecipeExtraction,
        uom_system: str,
        measure_preference: str,
    ) -> Optional[CayenneRefinement]:
        """Convert a raw RecipeExtraction into Cayenne Fat Token format."""
        ...

    @abstractmethod
    def categorize(
        self,
        recipe: RecipeExtraction,
        category_tree: "CategoryTree",
    ) -> List[str]:
        """Assign 1–3 category names from the provided taxonomy. Returns fallback on failure."""
        ...

    def normalize_baker_table(self, text: str) -> str:
        """Pre-process baker's percentage tables. Default: passthrough (no-op)."""
        return text
```

### Provider Factory

```python
# core/providers/factory.py
def create_provider(name: str, api_key: str, model: Optional[str] = None) -> LLMProvider:
    """
    Instantiate an LLMProvider by name.
    name: "gemini" | "openai" | "anthropic" | "mock"
    """
```

### Implemented Providers

| Provider | Class | Model | Status |
|----------|-------|-------|--------|
| `gemini` | `GeminiProvider` | `gemini-2.5-flash` | ✅ Default |
| `openai` | `OpenAIProvider` | `gpt-4o` | 🔲 Future |
| `anthropic` | `AnthropicProvider` | `claude-3-5-sonnet` | 🔲 Future |
| `mock` | `MockProvider` | n/a | ✅ Tests |

### Retry & Back-off

Each provider implementation is responsible for its own retry logic. The `GeminiProvider` uses the existing exponential back-off from `gemini.py` (`_call_with_retry`). Other providers implement equivalent logic appropriate to their SDK.

## 6. Embedding Strategy

Embedding is a separate concern from LLM extraction and uses its own provider interface.

```python
# core/providers/base.py (continued)

class EmbeddingProvider(ABC):
    """Abstract interface for vector embedding — independent of the LLM provider."""

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """
        Generate a fixed-dimension embedding vector for the given text.
        All implementations MUST return exactly `self.dimensions` floats.
        """
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """The output vector dimension. Must match the database schema."""
        ...
```

### Selected Model: Gemini `gemini-embedding-001`

| Property | Value |
|----------|-------|
| Model | `gemini-embedding-001` |
| Provider | Google Gemini (same SDK as LLM provider) |
| Output dimensions | **1536** (via `output_dimensionality=1536`) |
| Schema match | ✅ `vector(1536)` in Supabase + `sqlite-vec` |
| Cost | Included in Gemini API quota — no second API key required |
| Rationale | Already shipped in v3.0.2; reuses the existing Gemini client; eliminates the OpenAI SDK dependency and a second API key |

### Embedding Input

The text passed to `embedder.embed()` is a concatenation of the recipe title and ingredient names:

```python
embed_text = f"{recipe.title}. {', '.join(i.name for i in recipe.structured_ingredients)}"
```

This produces a semantically rich vector that captures both the dish identity and its key components, optimized for the hybrid search query pattern used in the Cayenne app.

### Embedding Provider Factory

```python
# core/providers/factory.py
def create_embedding_provider(name: str, api_key: str) -> EmbeddingProvider:
    """
    name: "gemini" | "mock"
    Default: "gemini" — reuses the same API key as the LLM provider.
    """
    match name.lower():
        case "gemini":
            from .gemini import GeminiEmbeddingProvider
            return GeminiEmbeddingProvider(api_key=api_key)
        case "mock":
            from .mock import MockEmbeddingProvider
            return MockEmbeddingProvider()
        case _:
            raise ValueError(f"Unknown embedding provider: '{name}'")
```

### Environment Configuration

```
# .env
LLM_PROVIDER=gemini
GOOGLE_API_KEY=AIza...           # Single key — used for both LLM and embedding
EMBEDDING_PROVIDER=gemini        # Default; reuses GOOGLE_API_KEY
```

The CLI and GUI read these from `.env`. The API adapter reads them from environment variables set at deploy time (Docker / Cloud Run). No second API key is required.

## 7. Input Readers

All input sources are normalized to a `SourceDocument` before the engine sees them. Readers live in `io/readers/` and are the adapter's responsibility to invoke.

### SourceReader ABC

```python
# io/readers/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class ImageAsset:
    filename: str
    data: bytes
    mime_type: str

@dataclass
class SourceDocument:
    text: str
    images: List[ImageAsset] = field(default_factory=list)
    source_url: Optional[str] = None

class SourceReader(ABC):
    @abstractmethod
    def read(self, source: str) -> SourceDocument:
        """
        Read the source and return a SourceDocument.
        source: file path, URL, or raw text depending on reader type.
        Raises RecipeParserError on unrecoverable read failure.
        """
        ...
```

### Reader Implementations

| Reader | Class | Input | Notes |
|--------|-------|-------|-------|
| `epub.py` | `EpubReader` | File path | Extracts text + images from EPUB spine |
| `pdf.py` | `PdfReader` | File path | Extracts text + embedded images from PDF |
| `url.py` | `UrlReader` | URL string | Fetches via `https://r.jina.ai/{url}`; no images |
| `text.py` | `TextReader` | Raw string | Passthrough; no images |
| `paprika.py` | `PaprikaReader` | File path | Handles both Paprika and Cayenne `.paprikarecipes` formats |

### Paprika Reader — Format Detection

The `PaprikaReader` inspects each recipe entry in the ZIP archive:

```python
# io/readers/paprika.py
def _detect_format(entry: dict) -> str:
    """Returns 'cayenne' if _cayenne_meta key present, else 'paprika'."""
    return "cayenne" if "_cayenne_meta" in entry else "paprika"
```

- **Cayenne format**: `_cayenne_meta` key present → extract `CayenneRecipe` directly, skip Gemini extraction/refinement. The adapter signals the engine to bypass those stages.
- **Paprika format**: No `_cayenne_meta` → flatten ingredients + directions to plain text → pass through full engine pipeline.

### Reader Selection (Adapter Logic)

```python
# adapters/cli.py (example)
def select_reader(source: str) -> SourceReader:
    if source.startswith("http"):
        return UrlReader()
    path = Path(source)
    match path.suffix.lower():
        case ".epub":   return EpubReader()
        case ".pdf":    return PdfReader()
        case ".paprikarecipes": return PaprikaReader()
        case ".txt":    return TextReader()
        case _: raise RecipeParserError(f"Unsupported source type: {path.suffix}")
```

## 8. Category Sources

Category taxonomy is injected into the engine by the adapter. The adapter fetches the taxonomy from its appropriate source and passes a `CategoryTree` object to `engine.extract()`.

### CategorySource ABC

```python
# io/category_sources/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple

# (leaf_name, parent_name_or_None)
CategoryEntry = Tuple[str, Optional[str]]

@dataclass
class CategoryTree:
    entries: List[CategoryEntry]

    @property
    def leaf_names(self) -> List[str]:
        """Flat list of all category names (used for LLM prompt)."""
        return [name for name, _ in self.entries]

class CategorySource(ABC):
    @abstractmethod
    def load(self) -> CategoryTree:
        """Load and return the category taxonomy. Raises RecipeParserError on failure."""
        ...
```

### Source Implementations

| Source | Class | Used By | Notes |
|--------|-------|---------|-------|
| `yaml_source.py` | `YamlCategorySource` | CLI, GUI | Reads `categories.yaml`; creates default if missing |
| `paprika_db_source.py` | `PaprikaDbCategorySource` | CLI, GUI | Reads from Paprika SQLite (`--sync-categories`) |
| `supabase_source.py` | `SupabaseCategorySource` | API | Queries `categories` table filtered by `user_id` |

### Adapter Responsibility

```python
# adapters/api.py (example)
async def process_job(job_id: str, user_id: str, request: IngestRequest):
    # 1. Load category taxonomy for this user from Supabase
    cat_source = SupabaseCategorySource(user_id=user_id)
    category_tree = cat_source.load()

    # 2. Run engine with injected taxonomy
    result = engine.extract(doc, category_tree)
```

```python
# adapters/cli.py (example)
def run(args):
    # Load from YAML by default; Paprika DB if --sync-categories was run
    cat_source = YamlCategorySource(path=args.categories or DEFAULT_CATEGORIES_PATH)
    category_tree = cat_source.load()
    result = engine.extract(doc, category_tree)
```

### Fallback Behavior

If the category source returns an empty tree (no categories configured), the engine's categorizer falls back to `["Uncategorized"]` for all recipes. This is logged as a warning, not an error.

## 9. Output Writers

Writers consume an `ExtractionResult` and produce a file on disk. They are the adapter's responsibility to invoke after the engine completes.

### RecipeWriter ABC

```python
# io/writers/base.py
from abc import ABC, abstractmethod
from pathlib import Path
from recipeparser.core.engine import ExtractionResult

class RecipeWriter(ABC):
    @abstractmethod
    def write(self, result: ExtractionResult, output_dir: Path) -> Path:
        """Write recipes to output_dir. Returns the path of the created file."""
        ...
```

### Cayenne ZIP Writer (`cayenne_zip.py`)

Output: `<title>_<timestamp>.cayennerecipes` — a ZIP archive containing:

```
<recipe_uid>.json      # CayenneRecipe JSON (one file per recipe)
manifest.json          # {"format": "cayenne", "version": 1, "recipe_count": N}
```

Each recipe JSON includes `image_url` (Supabase Storage URL). Images are NOT embedded in the ZIP — they live in Supabase Storage.

### Paprika ZIP Writer (`paprika_zip.py`)

Output: `<title>_<timestamp>.paprikarecipes` — a ZIP archive containing:

```
<recipe_uid>.paprikarecipe    # gzip-compressed JSON per Paprika 3 format
```

Each entry embeds the image bytes (base64) for Paprika compatibility. The entry also includes a `_cayenne_meta` key containing the full `CayenneRecipe` JSON, enabling lossless round-trip import back into Cayenne (Flow B — bypass Gemini).

### Writer Selection (CLI/GUI)

```bash
recipeparser cookbook.epub --format cayenne   # default
recipeparser cookbook.epub --format paprika
```

The GUI exposes a radio button: `○ Cayenne  ○ Paprika (legacy)`

## 10. Adapter Contracts

Each adapter is responsible for exactly these concerns — no more, no less:

| Concern | CLI | GUI | API |
|---------|-----|-----|-----|
| Select & invoke reader | ✅ | ✅ | ✅ |
| Load category source | ✅ YAML/PaprikaDB | ✅ YAML/PaprikaDB | ✅ Supabase |
| Instantiate providers via factory | ✅ | ✅ | ✅ |
| Register FSM observer | ✅ log | ✅ progress bar | ✅ Supabase job row |
| Call `engine.extract()` | ✅ | ✅ | ✅ (background task) |
| Upload images to Supabase Storage | ✅ | ✅ | ✅ |
| Invoke writer (format selection) | ✅ `--format` flag | ✅ radio button | ❌ (API returns JSON) |
| Return HTTP response | ❌ | ❌ | ✅ 202 + job_id |
| Write to Supabase `recipes` table | ❌ out of scope | ❌ out of scope | ✅ |

### API Adapter — Fire-and-Forget Flow

```
POST /jobs  →  202 { job_id }
                │
                └─ BackgroundTask:
                     1. fsm.transition(LOADING)   → UPDATE ingestion_jobs
                     2. reader.read(source)
                     3. fsm.transition(CHUNKING)  → UPDATE ingestion_jobs
                     4. engine.extract(doc, tree)  (FSM transitions fire internally)
                     5. Upload images → Supabase Storage
                     6. INSERT recipes + embeddings → Supabase
                     7. fsm.transition(DONE)      → UPDATE ingestion_jobs

GET /jobs/{job_id}  →  { status, stage, progress_pct, recipe_count, error }
```

## 11. Image Storage

All recipe images are stored in Supabase Storage. This is the single source of truth regardless of ingestion path.

### Bucket Layout

```
Bucket: recipe-images  (private)
Path:   {user_id}/{recipe_id}/{original_filename}
```

### Upload (Adapter Responsibility)

After `engine.extract()` returns, the adapter uploads each `ImageAsset` from `result.images` to Supabase Storage and stores the resulting public URL in the recipe's `image_url` field before writing to the output format.

### Output Format Behavior

| Format | Image in output | Image in Supabase |
|--------|----------------|-------------------|
| `.cayennerecipes` ZIP | `image_url` string only | ✅ Uploaded |
| `.paprikarecipes` ZIP | Bytes embedded (Paprika compat) + `image_url` in `_cayenne_meta` | ✅ Uploaded |
| API → Supabase INSERT | `image_url` in `recipes` row | ✅ Uploaded |

### RLS Policy

```sql
-- Users can only read/write their own images
CREATE POLICY "user_images" ON storage.objects
  FOR ALL USING (auth.uid()::text = (storage.foldername(name))[1]);
```

## 12. Ingestion Job Status (Supabase + PowerSync)

The API adapter writes FSM state transitions to an `ingestion_jobs` table in Supabase. PowerSync syncs this table to the local SQLite database on the mobile app, giving the user real-time progress visibility without polling.

### Supabase Table DDL

```sql
create table ingestion_jobs (
    id              uuid primary key default uuid_generate_v4(),
    user_id         uuid references auth.users not null,
    status          text not null default 'pending'
                    check (status in ('pending', 'running', 'done', 'error')),
    stage           text not null default 'IDLE'
                    check (stage in ('IDLE','LOADING','CHUNKING','EXTRACTING',
                                     'CATEGORIZING','REFINING','EMBEDDING','DONE','ERROR')),
    progress_pct    integer not null default 0 check (progress_pct between 0 and 100),
    recipe_count    integer not null default 0,
    source_hint     text,           -- e.g. "Ottolenghi Simple" or "https://..."
    error_message   text,
    created_at      timestamp with time zone default timezone('utc', now()),
    updated_at      timestamp with time zone default timezone('utc', now())
);

-- RLS
alter table ingestion_jobs enable row level security;
create policy "user_jobs" on ingestion_jobs
    for all using (auth.uid() = user_id);
```

### PowerSync Sync Rules (`sync-rules.yaml` addition)

```yaml
- table: ingestion_jobs
  parameters:
    - name: user_id
      value: token_parameters.user_id
  where: user_id = :user_id
```

### Local SQLite Migration (Cayenne app)

```sql
-- migrations/006_ingestion_jobs.sql
create table if not exists ingestion_jobs (
    id              text primary key,
    user_id         text not null,
    status          text not null,
    stage           text not null,
    progress_pct    integer not null,
    recipe_count    integer not null,
    source_hint     text,
    error_message   text,
    created_at      text not null,
    updated_at      text not null
);
```

### API Observer Implementation

```python
# adapters/api.py
def make_supabase_observer(job_id: str, supabase_client) -> Callable:
    def observer(fsm: ExtractionFSM) -> None:
        status = "running"
        if fsm.state == ExtractionState.DONE:
            status = "done"
        elif fsm.state == ExtractionState.ERROR:
            status = "error"
        supabase_client.table("ingestion_jobs").update({
            "status": status,
            "stage": fsm.state.name,
            "progress_pct": fsm.progress,
            "recipe_count": fsm.recipe_count,
            "error_message": fsm.error_message,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()
    return observer
```

### Cayenne App — `useIngestionJobs` Hook

```typescript
// src/hooks/useIngestionJobs.ts
export function useIngestionJobs(): IngestionJobRow[] {
  // Queries local SQLite via PowerSync — zero network calls
  return usePowerSyncQuery<IngestionJobRow>(
    "SELECT * FROM ingestion_jobs ORDER BY created_at DESC LIMIT 20"
  );
}
```

The Library screen shows a dismissible banner for any job with `status = 'running'`, and a success/error toast when `status` transitions to `done` or `error`.
