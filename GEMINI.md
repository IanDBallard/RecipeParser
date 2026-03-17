# Gemini CLI Context: RecipeParser

This project is a production-grade tool designed to extract recipes from EPUB and PDF cookbooks and export them as `.cayennerecipes` or `.paprikarecipes` archives. It leverages Google's **Gemini 2.5 Flash** model for structured data extraction, image matching, automatic categorization, and vector embedding via `gemini-embedding-001`.

> **Architecture Status:** The project is undergoing a big-bang refactor to a clean layered architecture. See `ARCHITECTURE.md` for the full design, `PROVIDER_GUIDE.md` for adding new LLM/embedding providers, and `MIGRATION_PLAN.md` for the cutover checklist. The descriptions below reflect the **target architecture**.

## Project Overview

-   **Domain:** Cookbook digitization and recipe management for Project Cayenne.
-   **Core Tech:** Python 3.9+, Gemini API (via `google-genai`), Pydantic (data validation), EbookLib/BeautifulSoup (EPUB), PyMuPDF (PDF), CustomTkinter (GUI), FastAPI/Uvicorn (API).
-   **Offline-First Principle (Nuanced):** RecipeParser is the AI-enhanced ingestion layer — it only runs when connectivity is available. The Cayenne mobile app is offline-first for its core recipe management function. The two tiers are:

    | Tier | Connectivity | Features |
    |---|---|---|
    | **Always offline** (Cayenne app) | None required | Browse library, view recipes, scale servings, kitchen mode, cross-off ingredients, search within already-synced recipes (sqlite-vec on local data), Paprika Flow B restore |
    | **Requires connectivity** | Internet + RecipeParser API | Ingest new recipes (AI extraction), generate new embeddings, auto-categorize new recipes, PowerSync sync, Paprika Flow A re-extraction |

    The Cayenne app must never degrade to a broken state when offline. RecipeParser is only invoked for AI-enhanced features; the app functions as a complete recipe manager without it.

-   **Key Features:**
    -   AI-driven extraction of recipe fields (ingredients, directions, etc.) via pluggable `LLMProvider` interface.
    -   Fat Token direction format (`{{ing_01|fallback}}`) for deterministic scaling in the Cayenne mobile app.
    -   Vector embedding via `gemini-embedding-001` (1536 dimensions) — same API key as LLM, no second key needed.
    -   "Hero image" detection and breadcrumb injection for accurate photo matching.
    -   Automatic categorization using a configurable taxonomy (YAML, Paprika DB, or Supabase).
    -   Parallel processing with built-in rate limiting (RPM/concurrency caps).
    -   Real-time ingestion progress via `ingestion_jobs` table synced to the Cayenne app via PowerSync.
    -   Windows GUI installer and cross-platform Python CLI.

## Building and Running

### Development Setup
1.  **Install in editable mode:**
    ```bash
    pip install -e .
    ```
2.  **API Key:** Store your Gemini API key in a `.env` file or `%APPDATA%\RecipeParser\.env`:
    ```env
    GOOGLE_API_KEY=your_key_here
    LLM_PROVIDER=gemini
    EMBEDDING_PROVIDER=gemini        # reuses GOOGLE_API_KEY — no second key needed
    SUPABASE_URL=https://your-project.supabase.co
    SUPABASE_SERVICE_KEY=eyJ...      # service role key (CLI/GUI image upload)
    ```

### Running the Application
-   **CLI:** `recipeparser path/to/cookbook.epub`
-   **GUI:** `recipeparser-gui`
-   **API:** `uvicorn recipeparser.adapters.api:app`

### Testing
-   **Run all tests:** `python -m pytest tests/`
-   **Note:** Tests use `MockProvider` + `MockEmbeddingProvider` for all AI interactions; no live API key is required.

### Windows Installer Build
-   **Requirements:** Inno Setup 6, PyInstaller.
-   **Command:** `powershell.exe .\build_installer.ps1`
-   **Configuration:** `recipeparser.spec` (PyInstaller) and `installer.iss` (Inno Setup).

## Architecture & Code Map

The project follows a strict layered architecture with a pure core engine, pluggable providers, and thin adapter wrappers. **No cross-layer imports** — core never imports from `io/` or `adapters/`.

```
recipeparser/
├── core/                          # Pure extraction engine — no I/O, no side effects
│   ├── engine.py                  # RecipeEngine — orchestrates the full pipeline
│   ├── chunker.py                 # Splits source text into processable segments
│   ├── fsm.py                     # ExtractionFSM — externalized state machine
│   └── providers/
│       ├── base.py                # LLMProvider + EmbeddingProvider ABCs
│       ├── factory.py             # create_provider() / create_embedding_provider()
│       ├── gemini.py              # GeminiProvider + GeminiEmbeddingProvider (gemini-embedding-001)
│       ├── openai.py              # OpenAIProvider (stub — future)
│       ├── anthropic.py           # AnthropicProvider (stub — future)
│       └── mock.py                # MockProvider + MockEmbeddingProvider (tests)
│
├── io/
│   ├── readers/                   # Source → SourceDocument(text, images)
│   │   ├── epub.py                # EPUB reader
│   │   ├── pdf.py                 # PDF reader
│   │   ├── url.py                 # URL reader (via r.jina.ai)
│   │   ├── text.py                # Plain text passthrough
│   │   └── paprika.py             # .paprikarecipes reader (Paprika + Cayenne formats)
│   ├── writers/                   # ExtractionResult → output file
│   │   ├── cayenne_zip.py         # .cayennerecipes ZIP (Cayenne JSON + image URLs)
│   │   └── paprika_zip.py         # .paprikarecipes ZIP (Paprika JSON + embedded images)
│   └── category_sources/          # Taxonomy → CategoryTree
│       ├── yaml_source.py         # Load from categories.yaml
│       ├── paprika_db_source.py   # Load from Paprika SQLite
│       └── supabase_source.py     # Load from Supabase categories table
│
├── adapters/                      # Environment-specific wrappers (thin — no business logic)
│   ├── cli.py                     # CLI entry point
│   ├── gui.py                     # CustomTkinter GUI wrapper
│   └── api.py                     # FastAPI wrapper (fire-and-forget /jobs endpoint)
│
├── models.py                      # Pydantic schemas — source of truth for all data shapes
├── config.py                      # Constants (retry limits, backoff, concurrency caps)
├── exceptions.py                  # RecipeParserError hierarchy
└── __main__.py                    # Entry point → adapters/cli.py
```

### Pipeline Sequence (inside `core/engine.py`)

```
SourceDocument.text
       │
       ▼
  [CHUNKING]   chunker.py → List[str]
       │
       ▼
  [EXTRACTING] llm.extract_recipes(chunk) → List[RecipeExtraction]  (concurrent)
       │
       ▼
  [DEDUP]      normalize names, remove duplicates
       │
       ▼
  [CATEGORIZING] llm.categorize(recipe, category_tree) → List[str]
       │
       ▼
  [REFINING]   llm.refine_recipe(raw) → CayenneRefinement  (Fat Tokens)
       │
       ▼
  [EMBEDDING]  embedder.embed(title + ingredients) → List[float]  (1536-dim)
       │
       ▼
  ExtractionResult
```

### FSM States

`IDLE → LOADING → CHUNKING → EXTRACTING → CATEGORIZING → REFINING → EMBEDDING → DONE / ERROR`

Each adapter registers its own FSM observer:
- **CLI:** logs state transitions
- **GUI:** updates progress bar
- **API:** writes to `ingestion_jobs` Supabase table → PowerSync → Cayenne app

### API Flow (fire-and-forget)

```
POST /jobs  →  202 { job_id }
                └─ BackgroundTask: read → extract → upload images → INSERT recipes → DONE
GET /jobs/{job_id}  →  { status, stage, progress_pct, recipe_count, error }
```

## Development Conventions

-   **Data Validation:** Always use the Pydantic models in `models.py` when handling recipe data. This is the canonical source of truth shared with the Cayenne mobile app.
-   **Provider Interface:** All LLM and embedding operations go through `LLMProvider` / `EmbeddingProvider` ABCs. Never import a concrete provider (e.g., `GeminiProvider`) outside of `core/providers/`. See `PROVIDER_GUIDE.md` for how to add a new provider.
-   **No Cross-Layer Imports:** `core/` must never import from `io/` or `adapters/`. Adapters import from both `core/` and `io/`.
-   **Error Handling:** Use the custom exception hierarchy in `exceptions.py`. Providers must return `None` (not raise) on transient API failures; adapters handle the `None` case.
-   **Configuration:** Tuneable constants (timeouts, retry counts, model names) are centralized in `config.py`.
-   **Concurrency:** Rate limiting is the responsibility of each provider implementation. `GeminiProvider` uses exponential back-off internally.
-   **Versioning:** `pyproject.toml` is the single source of truth for the version number. `installer.iss` must be manually updated to match for releases.
-   **Linting/Formatting:** Standard Python practices; use `pytest` for all functional verification.
-   **Fat Token format:** `{{ing_01|fallback string}}` — Group 1 = ingredient ID, Group 2 = fallback. The `refine_recipe` prompt must produce this format. IDs must be sequential (`ing_01`, `ing_02`, ...).
-   **`is_ai_converted`:** Must be `true` only for Volume-to-Weight conversions using ingredient density knowledge — not for unit normalization.
