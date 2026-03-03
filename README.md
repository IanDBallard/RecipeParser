# RecipeParser

A production-grade tool that extracts recipes from EPUB cookbooks and exports them as a `.paprikarecipes` archive ready to import into [Paprika 3](https://www.paprikaapp.com/).

Available in two forms:
- **Windows GUI installer** — a self-contained `RecipeParser-Setup-x.x.x.exe` that requires no Python installation
- **Python CLI/library** — installable via `pip` for scripting and automation

It uses Google's **Gemini 2.5 Flash** model to understand recipe structure, handle diverse book layouts, assign taxonomy categories, and intelligently match hero photographs — all without brittle regex or hard-coded formatting rules.

---

## Features

- **Extracts all standard recipe fields** — name, ingredients, directions, servings, prep time, cook time, and author notes
- **Embeds hero photographs** — matches cover photos to recipes using image breadcrumbs injected into the text, with a look-ahead injection mechanism for books that place photos on standalone pages before the recipe
- **Automatic categorisation** — assigns 1–3 Paprika taxonomy categories per recipe using the LLM, drawn from a user-configurable `categories.yaml` file
- **Built-in category editor** — a two-panel GUI editor lets you add, rename, reorder, and delete categories without touching YAML by hand
- **Unit-of-measure preference** — for dual-measurement books (e.g. `2 cups / 250g flour`), instructs the AI to keep only your preferred system (metric, US, or imperial)
- **Parallel processing** — extraction and categorisation both run concurrently with a configurable concurrency cap, with automatic exponential back-off on rate limits
- **Handles diverse EPUB structures** — prose recipes, ingredient lists, baker's percentage tables, multi-recipe chapters, and text-only historic cookbooks all work
- **Safe and robust** — per-task timeouts, typed custom exceptions, graceful degradation (a failed segment is skipped, not fatal), and image-less recipes export cleanly without crashing Paprika

---

## Windows Installer (GUI)

### Download and Install

1. Download `RecipeParser-Setup-2.0.1.exe` from the [Releases](https://github.com/IanDBallard/RecipeParser/releases) page (or build it yourself — see [Building the Windows Installer](#building-the-windows-installer) below).
2. Run the installer. During setup you will be prompted to enter your Google Gemini API key (get one free at [aistudio.google.com](https://aistudio.google.com/app/apikey)).
3. The key is written to `%APPDATA%\RecipeParser\.env` and survives upgrades.
4. A **RecipeParser** shortcut appears in the Start Menu (and optionally on the Desktop).

No Python installation required. The installer bundles the complete Python runtime and all dependencies.

### Using the GUI

The application has two tabs:

#### Parse Tab

| Control | Purpose |
|---|---|
| EPUB File | Browse to a `.epub` file or a Calibre book folder (the `.epub` is auto-detected) |
| Output Folder | Where the `.paprikarecipes` file will be written (default: `Documents\RecipeParser`) |
| Units | Unit-of-measure preference for dual-measurement books |
| Google API Key | Your Gemini key — click **Save** to persist it to `%APPDATA%\RecipeParser\.env` |
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

---

## CLI Usage

```bash
recipeparser path/to/cookbook.epub
```

The `.paprikarecipes` file is written to `Documents\RecipeParser` by default (or `%APPDATA%\RecipeParser\Exports` if Documents is unavailable). You can also pass a Calibre book folder — the single `.epub` inside it is detected automatically.

**Options:**

```
usage: recipeparser [-h] [--output DIR] [--units {metric,us,imperial,book}]
                    [--sync-categories] [epub]

positional arguments:
  epub                  Path to the .epub file, or a Calibre book folder containing one

options:
  --output DIR          Directory to write the .paprikarecipes file
  --units               Unit-of-measure preference for dual-measurement books.
                        metric   — keep gram/ml values only
                        us       — keep cup/tbsp/oz values only
                        imperial — keep oz/lb values only
                        book     — preserve whatever the book uses (default)
  --sync-categories     Pull the live category hierarchy from your local Paprika database
                        and save to the user categories file. No EPUB required.
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
```

Then in Paprika 3: **File → Import Recipes** and select the `.paprikarecipes` file.

### Launch the GUI from the command line

```bash
recipeparser-gui
```

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
├── gemini.py          All Gemini API calls — extraction, normalisation, retry logic
├── categories.py      YAML taxonomy loader and LLM categorisation
├── paths.py           User-writable paths (app data, categories, output)
├── pipeline.py        Orchestration — parallel execution, hero injection, dedup, export
├── export.py          Paprika 3 archive bundler
└── categories.yaml    Default recipe taxonomy

tests/
├── conftest.py
├── test_epub.py
├── test_gemini.py
├── test_export.py
├── test_categories.py
├── test_pipeline.py
├── test_paprika_db.py
└── test_cli.py

recipeparser.spec       PyInstaller build spec
installer.iss           Inno Setup installer script
build_installer.ps1     One-click build pipeline (PowerShell)
```

---

## Building the Windows Installer

Prerequisites (one-time):
- `pip install pyinstaller`
- [Inno Setup 6](https://jrsoftware.org/isdl.php) installed to its default location

Then from the project root in PowerShell:

```powershell
.\build_installer.ps1
```

This cleans previous artefacts, runs PyInstaller, compiles the Inno Setup script, and writes the finished installer to `output\RecipeParser-Setup-2.0.1.exe`.

---

## Running Tests

```bash
python -m pytest tests/ -v
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
- **Scanned / image-only EPUBs** contain no machine-readable text and cannot be processed.
- **DRM-protected EPUBs** cannot be opened by ebooklib. You must hold a legitimate copy in a DRM-free format (e.g. via your own Calibre library).
