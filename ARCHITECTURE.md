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
│   └── providers/                 # an empty __init__.py only: the provider layer of §5 and §6
│                                  # was designed and never built (2026-10-01)
│
├── io/
│   ├── readers/                   # Source → SourceDocument(text, images)
│   │   ├── base.py                # SourceReader ABC + SourceDocument dataclass
│   │   ├── epub.py                # EPUB reader
│   │   ├── pdf.py                 # PDF reader
│   │   ├── url.py                 # URL reader (via Jina r.jina.ai; direct fetch on failure)
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

> **Status, 2026-10-01: a design that was never built.** There is no `LLMProvider` ABC, `base.py` or `factory.py`; `core/providers/` holds an empty `__init__.py`. The Gemini calls are made directly, from `recipeparser/gemini.py` (extraction, refinement, embeddings), `recipeparser/categories.py` and `recipeparser/toc.py`. The models are `GEMINI_MODEL` and `GEMINI_EMBEDDING_MODEL` in `recipeparser/config.py`. This section is kept as the design it was.

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
| `gemini` | none; direct calls in `gemini.py` | `GEMINI_MODEL`, `gemini-3.1-flash-lite` by default (`gemini-2.5-flash` retires 2026-10-16) | ✅ The only one |
| `openai` | `OpenAIProvider` | `gpt-4o` | 🔲 Future |
| `anthropic` | `AnthropicProvider` | `claude-3-5-sonnet` | 🔲 Future |
| `mock` | `MockProvider` | n/a | ✅ Tests |

### Retry & Back-off

Each provider implementation is responsible for its own retry logic. The `GeminiProvider` uses the existing exponential back-off from `gemini.py` (`_call_with_retry`). Other providers implement equivalent logic appropriate to their SDK.

## 6. Embedding Strategy

> **Status, 2026-10-01: a design that was never built.** There is no `EmbeddingProvider` ABC or factory. Embedding is one function, `get_embeddings()` in `recipeparser/gemini.py`, using `GEMINI_EMBEDDING_MODEL` (`gemini-embedding-001`, 1536 dimensions).

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
| `url.py` | `UrlReader` | URL string | Fetches via `https://r.jina.ai/{url}`; when Jina fails, fetches the page directly and reads its schema.org Recipe JSON-LD, else its article text; no images |
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

### What the pinned model can and cannot judge

`gemini.TAGGING_RULES` — in `build_categorize_batch_prompt` (#88), which every path that tags now uses; until 9.8.0 the refine prompt carried it too — asks for a tag that is true of the dish as a whole. Measured on 2026-10-03 through the real refine path, over 14 recipes whose right answer is uncontroversial with five runs each (the numbers are in Cayenne's Fix Roadmap, F-205), the pinned `gemini-3.1-flash-lite` obeys those rules **only where an axis has a legitimate answer**. The fault is one move: it reaches for the nearest thing the recipe mentions when an axis would otherwise be empty. Chicken schnitzel and beef meatballs never take Egg, because Protein is already answered; gnocchi and a Victoria sponge always do.

9.6.4 names that move as the mistake and says an empty axis is one of the commonest right answers, which took the set from 133/160 assertions to 147/160. What that leaves:

- Reliable (100% across runs): a technique over a preliminary step — searing before a braise, boiling before a bake — and every positive control, including a dish that really is about eggs.
- Fixed by 9.6.4: pancakes no longer take Egg, French onion soup no longer takes Beef from its stock, and a chicken-stock risotto is no longer called Vegetarian.
- Still wrong, and not fixable by wording at this tier: eggs worked into a dough or a batter make gnocchi and the sponge Egg recipes, in every run.
- Unstable: the minestrone stopped taking Chicken for good, but claims Vegetarian in its place in three runs of five.

**How many axes one request offers is the variable, not where the request lives.** `scripts/retag_axis.py` offers **one axis per call** over batches of ten recipes; `refine_recipe_for_cayenne` offers every axis at once for a single recipe. Same model, same rules block: measured on 2026-10-03 the one-axis shape scored **105/105** on the sample taxonomy, returning an empty Protein axis for gnocchi and a Victoria sponge — the two cases the import path gets wrong in every single run. Offering the same six axes in one batch call reproduced the import path's failures exactly. So since 9.8.0 the import asks per axis too (Cayenne F-246): REFINE no longer tags, and the pipeline's TAG stage runs `categorize_batch` over every five finished recipes, one axis per call, at temperature 0.0. Five and not the retag's ten: measured on 2026-10-05 over a rebuilt fourteen-recipe sample, three runs each, one recipe per call scored 99/99, five per call 99/99, and ten 95/99, two stock-based soups batched beside vegetarian dishes taking `Vegetarian` in two runs of three. The retag now batches by five too. A batch's tags are written as links to rows already in the table (`write_recipe_categories`), and an axis whose call fails twice leaves only that axis empty and is counted with the job's refused links. A Cayenne restore is never tagged; it carries its own. An axis that mixes a property of the dish with a property of its ingredients is overtagged whatever the prompt says, and the fix for that shape is the caller's taxonomy: moving `Vegetarian` off the owner's Protein axis took it from 61.6% of the library to 43.4%, and `Egg` on that axis had to be fixed by a no-model pass instead.

The same prompts score 96/96 on `gemini-3.8-flash` and 89/96 on `gemini-3.5-flash`, so the residue is a capability limit rather than a prompt defect; five other wordings were tried and each cost a case that had been passing — including a generic "the dish's own name is the best evidence of what it is" rule, which regressed two cases on the import path and changed nothing on the retag path. The owner ruled on 2026-10-03 to stay on flash-lite, because `GEMINI_MODEL` drives every call in this service and not only tagging. Three things follow for this codebase. The two egg cases in `tests/goldens/test_tagging_golden.py` were strict xfails naming the model, split from the positive halves that still hold; since 9.8.0 the set runs through the TAG stage, which is what they are judged against. `scripts/retag_axis.py` must not be run as a blind per-axis replace on an affected axis, since it would drop a cook's own links and write the same overtags back. And `gemini-3.5-flash-lite` is not an upgrade path: it returned no usable refinement in 42 of 42 runs. When re-measuring the old import path, note that the refine call sampled at temperature 0.1, so a single observation of a tagging case was not a measurement; the TAG stage samples at 0.0.

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

GET /jobs/{job_id}  →  { job_id, status }
```

`GET /jobs/{job_id}` answers from the in-memory registry of running jobs (`JobStatusResponse`): `status` is the
pipeline controller's state, and a job that has finished, or is not the caller's, is a 404. The stage, progress,
recipe count and error live on the `ingestion_jobs` row, which is what Cayenne reads, by sync; the client calls
none of the job-status routes (Cayenne `SpecificationDocumentation/INGESTION_API.md`).

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

## 13. Recipe Edits and Regeneration

Spec: `docs/superpowers/specs/2026-09-07-recipe-edit-philosophy-design.md`.

The client edits **raw** columns only (`title`, `ingredient_lines`, `direction_steps`, metadata) and bumps `body_rev` on free-text changes. The server is the sole writer of **derived** columns (`structured_ingredients`, `tokenized_directions`, `embedding`, `derived_rev`). A recipe is stale when `derived_rev < body_rev`; stale rows are the queue.

| Module | Role |
|--------|------|
| `core/durations.py` | Deterministic prep/cook/servings parser. Shares `tests/fixtures/duration_cases.json` with the Cayenne TypeScript parser. |
| `core/regen.py` | Pure: REFINE input from a row, write-back payload, raw lines from derived data. |
| `adapters/regen_worker.py` | `RegenWorker.run_once()`: `claim_stale_recipes` RPC → REFINE → EMBED → `update … where id = ? and body_rev = ?`. Failures via `regen_failed` RPC. `run_workers()` is the shared poll loop. |
| `adapters/recat_worker.py` | `RecatWorker.run_once()`: one pending `ingestion_jobs` row with `kind = 'recategorize'` → batches of 10 recipes → `categorize_batch` (with `build_categorize_batch_prompt()`) → additive junction upserts. |

Workers start from the FastAPI lifespan when `REGEN_WORKER_ENABLED=1` and the service-role Supabase client is configured. Poll every 10 s; regen concurrency 2.

> **⚠️ Migration 013 is a deploy blocker, not a worker prerequisite.** `write_recipe_to_supabase` writes `ingredient_lines`, `direction_steps`, `body_rev`, `derived_rev`, `amount_overrides` and the nine duration/servings columns on **every** INSERT, unconditionally and behind no feature flag (`io/writers/supabase.py`). Against a pre-013 schema PostgREST rejects the row with `PGRST204` and ingest raises `RuntimeError` — **every ingest fails, for every user, on every path**, whether or not `REGEN_WORKER_ENABLED` is set. Apply Cayenne migration 013 (`recipe_edit_columns`) before deploying this version. Migration 014 (`regen_rpcs`) is required in addition before setting `REGEN_WORKER_ENABLED=1`, because `claim_stale_recipes` and `regen_failed` do not exist without it; their required semantics are recorded in `docs/sql/regen-rpcs.md`.

REFINE's `base_servings` is discarded on regen, being user-owned after ingest, and REFINE returns no tags since 9.8.0, so a regen leaves a recipe's categories as they are. `amount_overrides` is emptied on every successful regen because the new structured entries reflect the rewritten lines.

## 14. Recipe Sharing

Spec: Cayenne's `docs/superpowers/specs/2026-09-30-recipe-sharing-design.md`. Plans: Cayenne's `docs/superpowers/plans/2026-09-30-recipe-sharing-database.md` (the database) and `…-recipe-sharing-recipeparser.md` (this side).

| Module | Role |
|--------|------|
| `adapters/shares_api.py` | `build_router(verify, service_client, limiter)`: `POST /shares/recipient`, `POST /shares`, `POST /shares/{id}/accept`, `/decline`, `/cancel`. Each change is one call to a Cayenne `security definer` function executable by `service_role` only (`user_id_for_email`, `create_recipe_share`, `accept_recipe_share`, `close_recipe_share`); supabase-py cannot hold a transaction, so the function is the transaction. A refused transition reads the share once to answer 404 (not a party) or 409 (no longer pending). `RecipientCheckLimiter` holds D6's 20 lookups an hour per sender in memory. |
| `adapters/share_worker.py` | `ShareWorker.run_once()`: claims a `kind = 'share_accept'` job as `RecatWorker` claims a recategorise job (compare-and-swap; a `running` job untouched for `STALE_LEASE_MINUTES` is reclaimed). Then one item per poll: the picture read from the bucket and stored under `uuid5(SHARE_NAMESPACE, item id)`, then `copy_shared_item`, retried once; an item that still fails becomes `failed` and the rest go on. With no item left it calls `finish_recipe_share` and ends the job `done`. A database failure is raised, never turned into an `error` job, because that would leave the share in `accepting` for ever: `run_workers` counts the raised poll as idle and sleeps, the worker keeps the job and carries on at the next poll, and `/health`'s stamp for `ShareWorker` stops advancing while it fails. After a restart the lease brings the job back. The same poll runs `expire_recipe_shares` every 15 minutes and `purge_recipe_shares` daily. |

The job's `stage` stays `IDLE` until it ends: the stage check has no copying value, and the client follows the share's items, not the job.

`cleanup_jobs.py` marks every `pending` or `running` job `error` whatever its `kind`. Run by hand, it would leave any share being accepted in `accepting` for good, so do not run it while a `share_accept` job is open.
