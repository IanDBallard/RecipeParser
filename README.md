# RecipeParser

A production-grade tool that extracts recipes from **EPUB or PDF** cookbooks and exports them as a `.paprikarecipes` archive ready to import into [Paprika 3](https://www.paprikaapp.com/).

Available in three forms:
- **Windows GUI installer** — a self-contained `RecipeParser-Setup-x.x.x.exe` that requires no Python installation
- **Python CLI/library** — installable via `pip` for scripting and automation
- **Cayenne Ingestion API** — a FastAPI-powered endpoint for recipe refinement and vector embedding

It uses Google's **Gemini 2.5 Flash** model to understand recipe structure, handle diverse book layouts, assign taxonomy categories, and intelligently match hero photographs — all without brittle regex or hard-coded formatting rules.

---

## Features

- **Extracts all standard recipe fields** — name, ingredients, directions, servings, prep time, cook time, and author notes
- **Embeds hero photographs** — matches cover photos to recipes using image breadcrumbs injected into the text, with a look-ahead injection mechanism for books that place photos on standalone pages before the recipe
- **Automatic categorisation** — assigns 1–3 Paprika taxonomy categories per recipe using the LLM, drawn from a user-configurable `categories.yaml` file
- **Built-in category editor** — a two-panel GUI editor lets you add, rename, reorder, and delete categories without touching YAML by hand
- **Unit-of-measure preference** — for dual-measurement books (e.g. `2 cups / 250g flour`), instructs the AI to keep only your preferred system (metric, US, or imperial)
- **Parallel processing** — extraction and categorisation both run concurrently with a configurable concurrency cap, with automatic exponential back-off on rate limits
- **Handles diverse EPUB and PDF structures** — prose recipes, ingredient lists, baker's percentage tables, multi-recipe chapters, and text-only historic cookbooks all work; PDFs are supported with pre-flight checks and page-based extraction
- **TOC-based reconciliation** — extracts table of contents (EPUB nav/NCX or PDF outline) when present, compares it to extracted recipes, and logs any missed or extra recipes; extraction always uses page/document chunking for best results
- **Recipe Refinement (Cayenne)** — converts raw recipes into structured JSON with normalized ingredients and "Fat Token" directions, including 1536-dimension vector embeddings using `text-embedding-004`
- **Safe and robust** — per-task timeouts, typed custom exceptions, graceful degradation (a failed segment is skipped, not fatal), and image-less recipes export cleanly without crashing Paprika

---

## Windows Installer (GUI)

### Download and Install

1. Download `RecipeParser-Setup-2.1.1.exe` from the [Releases](https://github.com/IanDBallard/RecipeParser/releases) page (or build it yourself — see [Building the Windows Installer](#building-the-windows-installer) below).
2. Run the installer. During setup you will be prompted to enter your Google Gemini API key (get one free at [aistudio.google.com](https://aistudio.google.com/app/apikey)).
3. The key is written to `%APPDATA%\RecipeParser\.env` and survives upgrades.
4. A **RecipeParser** shortcut appears in the Start Menu (and optionally on the Desktop).

No Python installation required. The installer bundles the complete Python runtime and all dependencies.

### Using the GUI

The application has two tabs:

#### Parse Tab

| Control | Purpose |
|---|---|
| Cookbook (EPUB or PDF) | Browse to an `.epub` or `.pdf` file, or a folder containing one |
| Output Folder | Where the `.paprikarecipes` file will be written (default: `Documents\RecipeParser`) |
| Units | Unit-of-measure preference for dual-measurement books |
| Google API Key | Your Gemini key — click **Save** to persist it to `%APPDATA%\RecipeParser\.env` |
| **Free tier** | Check to limit to 5 requests/min (default on). Uncheck to use the Concurrency setting. |
| **Concurrency** | Max in-flight API calls (1–10, default 1). Enabled when Free tier is unchecked. |
| Parse Recipes | Starts the extraction pipeline; progress streams live to the log panel |
| Open Output Folder | Opens the output folder in Explorer after a successful run |

#### Categories Tab

A two-panel editor for the recipe taxonomy used during AI categorisation:

- **Left panel** — top-level categories. Use ＋ / ✎ / ↑ / ↓ / ✕ to manage them.
- **Right panel** — subcategories of the selected parent. Same controls.
- **Save Changes** — writes edits to `%APPDATA%\RecipeParser\categories.yaml` (user-writable, survives upgrades). Changes take effect on the next parse run.
- **Import YAML / Export YAML** — load a taxonomy from an external file or save your current one as a backup.
- **Sync from Paprika** — reads the live category hierarchy directly from your local Paprika SQLite database and loads it into the editor, replacing the current taxonomy. Useful for keeping RecipeParser in sync with categories you have already created inside Paprika.

### Uninstalling

Use **Windows Settings → Apps → RecipeParser → Uninstall**. Program files are removed; your API key in `%APPDATA%\RecipeParser\` is left intact by default (you are offered the option to delete it).

---

## Python CLI Installation

### Prerequisites

- Python 3.9 or later
- A [Google AI Studio](https://aistudio.google.com/) API key with the Generative Language API enabled (free tier is sufficient)

**Gemini free tier:** The free tier allows **5 requests per minute** per model. Use **`--rpm 5`** (or the GUI’s “Free tier” checkbox) to cap request starts per minute; concurrency is capped at **10**. When **`--rpm`** is set, it is the constraining factor: e.g. `--rpm 10 --concurrency 10` with all 10 calls finishing in 10 seconds will sleep 50 seconds before the next batch.

### Install

Clone the repository and install in editable mode. This registers both the `recipeparser` CLI command and the `recipeparser-gui` GUI launcher globally:

```bash
git clone https://github.com/IanDBallard/RecipeParser.git
cd RecipeParser
pip install -e .
```

### Configure

Create a `.env` file in the project root (or export the variable into your shell environment):

```
GOOGLE_API_KEY=your_api_key_here
```

**Git hooks (optional):** If you push release tags (`v*`), enable the version-check hook so that `pyproject.toml` and `installer.iss` must match the tag (see [.githooks/README.md](.githooks/README.md)):

```bash
git config core.hooksPath .githooks
```

---

## CLI Usage

```bash
recipeparser path/to/cookbook.epub
# or
recipeparser path/to/cookbook.pdf
```

The `.paprikarecipes` file is written to `Documents\RecipeParser` by default (or `%APPDATA%\RecipeParser\Exports` if Documents is unavailable). You can pass a path to an `.epub` or `.pdf` file, or a directory containing exactly one `.epub` or one `.pdf` (e.g. a Calibre book folder).

**Options:**

```
usage: recipeparser [-h] [--output DIR] [--units {metric,us,imperial,book}]
                    [--sync-categories] [--concurrency N] [--rpm N] [epub]

positional arguments:
  epub                  Path to an .epub or .pdf cookbook, or a directory containing one

options:
  --output DIR          Directory to write the .paprikarecipes file
  --units               Unit-of-measure preference for dual-measurement books.
                        metric   — keep gram/ml values only
                        us       — keep cup/tbsp/oz values only
                        imperial — keep oz/lb values only
                        book     — preserve whatever the book uses (default)
  --sync-categories     Pull the live category hierarchy from your local Paprika database
                        and save to the user categories file. No EPUB required.
  --concurrency N       Max in-flight API calls (1–10, default 1). When --rpm is set,
                        RPM is the constraining factor.
  --rpm N               Requests per minute limit. When set, no more than N requests
                        start in any 60s window. Omit for no RPM cap.
```

**Examples:**

```bash
# Standard extraction
recipeparser "The Woks of Life.epub"

# Metric units for a dual-measurement baking book
recipeparser "Classic German Baking.epub" --units metric

# Pass a Calibre folder directly
recipeparser "C:\Calibre Library\Ken Forkish\The Elements of Pizza (621)"

# Write to a specific folder
recipeparser "Ottolenghi Simple.epub" --output ~/Desktop/paprika_imports

# Sync categories from your live Paprika database (no EPUB needed)
recipeparser --sync-categories

# Paid tier: higher concurrency and optional RPM cap
recipeparser "Big Cookbook.epub" --concurrency 10 --rpm 60
```

Then in Paprika 3: **File → Import Recipes** and select the `.paprikarecipes` file.

### Launch the GUI from the command line

```bash
recipeparser-gui
```

---

## Cayenne Ingestion API

The project includes a FastAPI server for high-fidelity recipe extraction and vector search indexing, used by the [Project Cayenne](https://github.com/iandballard/cayenne) mobile app.

### Starting the Server

```bash
uvicorn recipeparser.api:app --host 0.0.0.0 --port 8000
```

**Required environment variables:**

| Variable | Description |
|---|---|
| `GOOGLE_API_KEY` | Google AI Studio key for Gemini API calls |
| `SUPABASE_JWT_SECRET` | JWT secret from Supabase dashboard → Project Settings → API → JWT Secret |

### Authentication

All API endpoints require a valid Supabase JWT in the `Authorization` header:

```
Authorization: Bearer <supabase_access_token>
```

The server verifies the token using HS256 with the `SUPABASE_JWT_SECRET`. Requests without a valid token receive HTTP 401.

### Endpoints

#### `POST /ingest`

Accepts raw recipe text and returns a refined `CayenneRecipe` object with a vector embedding.

> **Note:** URL ingestion is not yet implemented. The `url` field is accepted but returns HTTP 400. Only `text` ingestion is currently supported.

**Request Body:**
```json
{
  "text": "1 cup flour, 2 eggs. Mix and bake at 350F for 20 mins.",
  "uom_system": "US",
  "measure_preference": "Volume"
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `text` | string | required | Raw recipe text to ingest |
| `url` | string | null | URL to scrape (not yet implemented) |
| `uom_system` | string | `"US"` | `"US"`, `"Metric"`, or `"Imperial"` |
| `measure_preference` | string | `"Volume"` | `"Volume"` or `"Weight"` |

**Response** (`IngestResponse`):
- `structured_ingredients`: List of objects with `id`, `amount`, `unit`, `name`, `fallback_string`, `converted_amount`, `converted_unit`, `is_ai_converted`.
- `tokenized_directions`: Steps with embedded Fat Token references (`{{ing_01|fallback text}}`).
- `embedding`: 1536-float vector from `text-embedding-004`.

**3-step pipeline:**
1. **Extraction** — `extract_recipe_from_text()` → raw `RecipeList` via Gemini 2.5 Flash
2. **Refinement** — `refine_recipe_for_cayenne()` → `CayenneRefinement` with Fat Tokens + unit conversions
3. **Vectorisation** — `get_embeddings()` → 1536-dimension embedding from `text-embedding-004`

#### `POST /embed`

Stand-alone endpoint to vectorize a search query string for semantic search.

**Request Body:**
```json
{
  "text": "citrus and refreshing"
}
```

**Response:**
```json
{
  "embedding": [0.123, -0.456, ...]
}
```

Returns a 1536-float vector. Used by the Cayenne mobile app's `useHybridSearch` hook.

---

## Python Library API

```python
from recipeparser import process_epub
from recipeparser.exceptions import RecipeParserError

try:
    output_path = process_epub(
        "path/to/cookbook.epub",
        output_dir="./output",
        units="metric",
    )
    print(f"Exported to: {output_path}")
except RecipeParserError as e:
    print(f"Failed: {e}")
```

---

## How It Works

### 1. EPUB Parsing
The EPUB is opened with `ebooklib`. Each HTML chapter is parsed with BeautifulSoup: `<img>` tags are replaced with plain-text `[IMAGE: filename.jpg]` breadcrumb markers, then the HTML is stripped to plain text. Images smaller than 20 KB (decorative separators, icons) are discarded; qualifying photos are extracted to a temporary directory.

### 2. Hero-Image Look-Ahead Injection
Some books (e.g. Paul Hollywood's *Pies & Puds*) place the hero photograph on a standalone page immediately before the recipe text. That tiny page contains no recipe content, so it would normally be skipped. The pipeline detects these "image-only stubs" and prepends the image filename as a `[HERO IMAGE: ...]` marker into the following recipe chunk, giving the LLM a definitive signal.

### 3. Recipe Candidate Filtering
A fast heuristic (`is_recipe_candidate`) checks each chunk for the co-presence of quantity keywords (`cup`, `tbsp`, `gram`, `ml`, `oz`, etc.) and structural keywords (`preheat`, `bake`, `stir`, `method`, `ingredients`, etc.) before any API call is made. Table-of-contents pages, author bios, glossaries, and copyright pages are all rejected without spending any API quota.

### 4. Pre-Normalisation for Complex Tables
Cookbooks that use baker's percentage tables (columnar `INGREDIENT / QUANTITY / BAKER'S %` layouts — common in professional bread books) are detected and sent through a separate Gemini normalisation pass before extraction. The table is converted to a readable `ingredient: quantity — percentage` format that the extraction prompt can handle reliably.

### 5. Parallel Recipe Extraction
All candidate chunks are submitted to a `ThreadPoolExecutor` simultaneously. A `threading.Semaphore` caps the number of in-flight Gemini API calls at any one time (default: 5, matching the free-tier limit). The results are reassembled in original chapter order after all futures complete. Each future has an individual timeout; timed-out or errored segments are logged and skipped without aborting the run.

### 6. Deduplication
Recipes are deduplicated by a case-folded, whitespace-normalised version of their name. The first occurrence (earliest chapter) is kept.

### 7. Parallel Categorisation
Each recipe is sent to Gemini in a parallel categorisation pass (same concurrency model as extraction). The model is given the full hierarchical taxonomy from `categories.yaml` and instructed to return 1–3 leaf category names. Invalid or hallucinated category names are filtered out; recipes with no valid assignment fall back to `EPUB Imports`.

### 8. Paprika Export
Each recipe is serialised as a JSON object matching Paprika 3's import schema, then gzip-compressed and written as a `.paprikarecipe` entry inside a ZIP archive. Hero photographs are base64-encoded and embedded directly. Recipes with no matched photo export cleanly with the photo fields omitted.

The temporary image directory is deleted after a successful export, and preserved for inspection if the export fails.

---

## User Data Locations

All user-writable files live in platform-appropriate directories to avoid permission issues when the app is installed in system-protected locations (e.g. Program Files):

| File | Location (Windows) | Purpose |
|------|--------------------|---------|
| `.env` | `%APPDATA%\RecipeParser\.env` | Google API key (CLI and GUI both read/write here) |
| `categories.yaml` | `%APPDATA%\RecipeParser\categories.yaml` | Recipe taxonomy; on first run, created with a minimal default (EPUB Imports) |
| Output (default) | `%USERPROFILE%\Documents\RecipeParser` | Where `.paprikarecipes` exports are written by default |

For Python CLI development, a `.env` file in the project root is also loaded and can override the app data value.

---

## Customising Categories

The taxonomy used for categorisation is stored in the user data directory (see above). You can edit it via the GUI's **Categories** tab or directly in the file:

```yaml
categories:
  - Soup
  - Mains:
      - Chicken Dishes
      - Beef Dishes
      - Pork Dishes
  - Dessert:
      - Cake
      - Pie
  - EPUB Imports
```

Top-level entries and subcategories are both valid Paprika category names. The `EPUB Imports` entry is the fallback used when the model cannot assign a confident category.

---

## Package Structure

```
recipeparser/
├── __init__.py        Public API — process_epub()
├── __main__.py        CLI entry point (argparse)
├── gui.py             CustomTkinter GUI — parse window + category editor
├── paprika_db.py      Paprika SQLite reader — category sync
├── config.py          All tuneable constants in one place
├── exceptions.py      Typed exception hierarchy
├── models.py          Pydantic schema for structured Gemini output
├── epub.py            EPUB parsing, image extraction, chunking, candidate filtering
├── pdf.py             PDF loading, pre-flight, page-based extraction
├── toc.py             TOC extraction (EPUB/PDF) and recon
├── gemini.py          All Gemini API calls — extraction, normalisation, retry logic
├── categories.py      YAML taxonomy loader and LLM categorisation
├── paths.py           User-writable paths (app data, categories, output)
├── pipeline.py        Orchestration — parallel execution, hero injection, dedup, export
├── export.py          Paprika 3 archive bundler
├── api.py             FastAPI server — /ingest and /embed endpoints (Cayenne Ingestion API)
└── categories.yaml    Default recipe taxonomy

tests/
├── conftest.py
├── test_epub.py
├── test_gemini.py
├── test_export.py
├── test_categories.py
├── test_pipeline.py
├── test_paprika_db.py
├── test_toc.py
└── test_cli.py

recipeparser.spec       PyInstaller build spec
installer.iss           Inno Setup installer script
build_installer.ps1     One-click build pipeline (PowerShell)
```

---

## Build & Release Pipeline

**GitHub Actions is the source of truth for releases.** The workflow builds on a fixed environment (Python 3.11, pinned deps) so the installer is consistent across all contributors, regardless of local Python version. To publish a release: bump the version, commit, tag, and push — CI does the rest.

The local `build_installer.ps1` script is for development and testing only. It does not create releases.

---

### Architecture Overview

```
pyproject.toml          ← single source of truth for the version number
installer.iss           ← must have matching AppVersion (CI validates this)
requirements.txt        ← pinned runtime deps (used by CI for reproducible builds)
recipeparser.spec       ← PyInstaller bundle configuration
build_installer.ps1     ← local build-only script (dev/testing; no release)
tests/smoke_test_exe.py   ← post-build exe smoke test (run by CI, also runnable locally)
tests/smoke_test_docker.py ← post-build Docker image smoke test (run by CI on tag, also runnable locally)
.github/workflows/
  build-installer.yml   ← GitHub Actions CI/CD pipeline
```

The pipeline has five jobs:

```
Any push / PR
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  Job 1: test  (ubuntu-latest, Python 3.11 + 3.12)  │
│                                                     │
│  • pip install -r requirements.txt + pytest         │
│  • pytest tests/ --cov=recipeparser                 │
│  • Runs on EVERY push and pull request              │
│  • Fast feedback — no Windows runner needed         │
└─────────────────────────────────────────────────────┘
      │  (tag push or workflow_dispatch only)
      ├─────────────────────────────────────────────────┐
      ▼                                                 ▼
┌─────────────────────────────┐  ┌─────────────────────────────────────┐
│  Job 2: build               │  │  Job 3a: smoke-test-docker           │
│  (windows-latest)           │  │  (ubuntu-latest)                     │
│                             │  │                                     │
│  1. Validate versions       │  │  • python tests/smoke_test_docker.py │
│  2. pip install deps        │  │  • Validates Dockerfile builds       │
│  3. pyinstaller + Inno Setup│  │  • Runs in parallel with build       │
│  4. Upload artifacts        │  │                                     │
└─────────────────────────────┘  └─────────────────────────────────────┘
      │                                          │
      │  (only if build succeeds)        │
      ▼                                  │
┌─────────────────────────────────────────────────────┐
│  Job 3b: smoke-test  (windows-latest)               │
│                                                     │
│  Downloads the built bundle artifact and runs       │
│  tests/smoke_test_exe.py against it:                │
│  • Exe exists and is > 20 MB                        │
│  • --help exits 0 and mentions 'epub'               │
│  • --version exits 0 and prints correct version     │
│  • Bad epub path exits non-zero with error message  │
└─────────────────────────────────────────────────────┘
      │  (only if exe smoke test passes)
      │  (release also requires smoke-test-docker to pass)
      ▼
┌─────────────────────────────────────────────────────┐
│  Job 4: release  (ubuntu-latest)                    │
│                                                     │
│  1. Download installer artifact                     │
│  2. Create/update GitHub Release with auto notes    │
│  3. Attach RecipeParser-Setup-{version}.exe         │
└─────────────────────────────────────────────────────┘
```

**Key principle:** the installer is never published unless the exe has been launched and verified to work correctly. A build-introduced regression (missing module, wrong version, broken CLI) will be caught by the smoke test before any user can download it.

---

### Releasing a New Version (Automated — Recommended)

**Step 1 — Bump the version in exactly two files:**

```
pyproject.toml   →  version = "2.1.1"
installer.iss    →  #define AppVersion "2.1.1"
```

> The CI workflow validates these match before building. If they differ it fails immediately with a clear error message.

**Step 2 — Commit, tag, and push:**

```bash
git add pyproject.toml installer.iss
git commit -m "Bump version to 2.1.1"
git tag v2.1.1
git push origin master --tags
```

**Step 3 — GitHub Actions takes over automatically:**

- Builds the PyInstaller bundle on `windows-latest`
- Validates customtkinter assets are present in the bundle
- Compiles the Inno Setup installer
- Creates a GitHub Release named `v2.1.1` with auto-generated release notes
- Attaches `RecipeParser-Setup-2.1.1.exe` to the release

The installer is also saved as a workflow artifact for 30 days — accessible from the Actions tab even if the release step fails.

**Manual trigger (without a new tag):**

Go to **Actions → Build Windows Installer → Run workflow** and enter the existing tag name. Useful for re-running a failed release without re-tagging.

---

### Dependency Management

Dependencies are declared in two places with different purposes:

| File | Purpose | Used by |
|------|---------|---------|
| `pyproject.toml` `[project.dependencies]` | Minimum version constraints for pip users | `pip install recipeparser` |
| `requirements.txt` | Exact pinned versions for reproducible builds | CI workflow, local builds |

**Both files must be kept in sync.** When adding or updating a dependency:

1. Update the version constraint in `pyproject.toml`
2. Update the pinned version in `requirements.txt`
3. If it's a GUI dependency, verify it's also covered in `recipeparser.spec`

Current pinned versions (`requirements.txt`):

```
EbookLib==0.18
beautifulsoup4==4.12.2
customtkinter==5.2.2
lxml==5.3.1
pydantic==2.11.9
google-genai>=1.60.0
python-dotenv==1.1.1
pyyaml==6.0.1
pymupdf>=1.24.0
fastapi>=0.115.0
uvicorn>=0.30.0
httpx>=0.27.0
PyJWT>=2.8.0
```

---

### PyInstaller Bundle (`recipeparser.spec`)

The spec uses **directory mode** (not `--onefile`) for fast launch times. The output is `dist\RecipeParser\` — a folder containing the `.exe` and all dependencies.

**CustomTkinter packaging** is the most fragile part of the bundle. CustomTkinter ships theme JSON files, fonts, and image assets that PyInstaller's static analyser cannot discover automatically. The spec handles this explicitly:

```python
# collect_all captures: datas (themes/fonts), binaries, hiddenimports
_d, _b, _h = collect_all("customtkinter")
datas += _d
ctk_binaries = _b
hiddenimports_ctk = _h

# darkdetect is a hard runtime dependency of customtkinter
# (used for OS light/dark mode detection — must be bundled separately)
_d, _b, _h = collect_all("darkdetect")
datas    += _d
ctk_binaries += _b
hiddenimports_ctk += _h
```

The CI workflow validates the bundle after PyInstaller runs:
- `dist\RecipeParser\customtkinter\` directory must exist
- At least one `.json` theme file must be present inside it
- `darkdetect` must be present in the bundle

If any of these checks fail, the build stops before wasting time on Inno Setup.

**Other packages requiring explicit collection:**

| Package | Why |
|---------|-----|
| `lxml` | Native C extensions; static analysis misses sub-modules |
| `grpc` | Native DLLs not found by static analysis |
| `google.api_core`, `google.protobuf` | Protobuf native extensions |
| `customtkinter` | Theme/font data files + hidden sub-modules |
| `darkdetect` | Runtime dep of customtkinter, not imported directly |

---

### Local Build (Developer — build only, no release)

For quick iteration when testing PyInstaller or Inno Setup changes. The output is suitable for local testing only — **it does not publish a release**.

Prerequisites (one-time setup):

1. **Python from [python.org](https://www.python.org/downloads/)** — must include tcl/tk (checked by default). PlatformIO/embedded Python lacks `tkinter` and will fail the preflight check.
2. **Inno Setup 6** — download from [jrsoftware.org/isdl.php](https://jrsoftware.org/isdl.php), install to the default location.
3. Install Python dependencies:
   ```powershell
   pip install -r requirements.txt
   pip install -e . pyinstaller
   ```

**Run the build:**

```powershell
.\build_installer.ps1
```

The script cleans `dist\`, `build\`, and `output\`, runs PyInstaller, then compiles the installer with Inno Setup. Output: `output\RecipeParser-Setup-{version}.exe`. No release is created — for that, push a version tag and let GitHub Actions run.

---

### Troubleshooting

**`tkinter` not found / `customtkinter` not found**
> You are using a Python that does not include the GUI libraries (e.g. PlatformIO, Conda minimal, or a system Python on some Linux distros). Install Python from [python.org](https://www.python.org/downloads/) and ensure "tcl/tk and IDLE" is checked during installation.

**`No module named customtkinter` in the built `.exe`**
> The `collect_all("customtkinter")` call in `recipeparser.spec` failed to find the package. Ensure `customtkinter` is installed in the same Python environment that runs PyInstaller: `pip install customtkinter==5.2.2`. The CI workflow has an explicit pre-build import check and a post-build asset validation step to catch this.

**`FileNotFoundError` for a theme file when the `.exe` launches**
> CustomTkinter's theme JSON files were not bundled. This means `collect_all("customtkinter")` ran but the package's data files were not found. Check that `customtkinter` is properly installed (not just importable — it must have its `assets/` directory). Run `python -c "import customtkinter; print(customtkinter.__file__)"` and verify the directory contains `assets/`.

**`darkdetect` import error at runtime**
> `darkdetect` is a hidden dependency of `customtkinter` used for OS theme detection. It is now explicitly collected in `recipeparser.spec` via `collect_all("darkdetect")`. If you see this error, ensure you are using the latest `recipeparser.spec`.

**Version mismatch error in CI**
> `pyproject.toml` and `installer.iss` have different version numbers. Update both to the same value before pushing the tag.

**Installer `.exe` not found after Inno Setup**
> The `OutputBaseFilename` in `installer.iss` is `RecipeParser-Setup-{AppVersion}`. If `AppVersion` in `installer.iss` does not match the version in `pyproject.toml`, the CI verification step will catch this. Locally, check the `output\` directory for what was actually produced.

---

## Running Tests

**Regression tests (pytest):**
```bash
python -m pytest tests/ -v
```
Excludes `smoke_test_exe.py` and `smoke_test_docker.py` (run by CI separately).

**Smoke tests (standalone, optional):**
```bash
python tests/smoke_test_docker.py   # Validate Docker image builds
python tests/smoke_test_exe.py dist\RecipeParser\RecipeParser.exe 2.2.0  # After PyInstaller build
```

All Gemini interactions are mocked — no live API calls or API key required.

---

## Notes on Photo Coverage

Photo coverage varies significantly by book and is driven entirely by the EPUB's own image-to-recipe ratio:

- **Modern photographed cookbooks** (e.g. *The Woks of Life*, *Paul Hollywood's Pies & Puds*) typically have one hero photo per recipe and achieve 80–100% coverage.
- **Classic/text-heavy cookbooks** (e.g. *An Invitation to Indian Cooking*, *Italian Food*) use sparse plate photography to illustrate chapters rather than individual recipes. Coverage of 15–40% on these books is expected and correct — the parser is not missing photos that exist.

The `[HERO IMAGE:]` look-ahead mechanism specifically addresses books where the photo appears on a standalone page before the recipe, rather than inline with the recipe text.

---

## Known Limitations

- **Baker's percentage tables** in very long chapters (> ~76,000 characters after normalisation) may still fail to extract fully. This is a planned improvement.
- **Scanned / image-only EPUBs and PDFs** contain no machine-readable text and cannot be processed. PDFs without a text layer fail pre-flight.
- **DRM-protected EPUBs** and **password-protected PDFs** cannot be opened. You must hold a legitimate copy in a DRM-free format (e.g. via your own Calibre library).
