# Gemini CLI Context: RecipeParser

This project is a production-grade tool designed to extract recipes from EPUB and PDF cookbooks and export them as `.paprikarecipes` archives for [Paprika 3](https://www.paprikaapp.com/). It leverages Google's **Gemini 2.5 Flash** model for structured data extraction, image matching, and automatic categorization.

## Project Overview

-   **Domain:** Cookbook digitization and recipe management.
-   **Core Tech:** Python 3.9+, Gemini API (via `google-genai`), Pydantic (data validation), EbookLib/BeautifulSoup (EPUB), PyMuPDF (PDF), CustomTkinter (GUI).
-   **Key Features:**
    -   AI-driven extraction of recipe fields (ingredients, directions, etc.).
    -   "Hero image" detection and breadcrumb injection for accurate photo matching.
    -   Automatic categorization using a configurable taxonomy or sync from a local Paprika DB.
    -   Parallel processing with built-in rate limiting (RPM/concurrency caps).
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
    ```

### Running the Application
-   **CLI:** `recipeparser path/to/cookbook.epub`
-   **GUI:** `recipeparser-gui`

### Testing
-   **Run all tests:** `python -m pytest tests/`
-   **Note:** Tests use mocks for Gemini interactions; no live API key is required.

### Windows Installer Build
-   **Requirements:** Inno Setup 6, PyInstaller.
-   **Command:** `powershell.exe .\build_installer.ps1`
-   **Configuration:** `recipeparser.spec` (PyInstaller) and `installer.iss` (Inno Setup).

## Architecture & Code Map

The project follows a modular pipeline pattern:

-   `recipeparser/pipeline.py`: Central orchestrator (LOAD -> EXTRACT -> RECON -> EXPORT).
-   `recipeparser/gemini.py`: API interaction, retry logic, and specialized normalization (e.g., Baker's percentage tables).
-   `recipeparser/models.py`: Pydantic schemas defining the contract between AI output and internal data.
-   `recipeparser/epub.py` & `recipeparser/pdf.py`: Format-specific extraction and image handling.
-   `recipeparser/export.py`: Handles ZIP/GZIP bundling for the `.paprikarecipes` format.
-   `recipeparser/paprika_db.py`: Reads categories from the local Paprika SQLite database.
-   `recipeparser/gui.py`: CustomTkinter interface for parsing and category management.

## Development Conventions

-   **Data Validation:** Always use the Pydantic models in `models.py` when handling recipe data to ensure consistency.
-   **Error Handling:** Use the custom exception hierarchy in `exceptions.py`. Avoid generic `Exception` catches in the core pipeline.
-   **Configuration:** Tuneable constants (timeouts, retry counts, model names) are centralized in `config.py`.
-   **Concurrency:** Use the `_RPMRateLimiter` in `gemini.py` for any new AI-interacting components to stay within quota.
-   **Versioning:** `pyproject.toml` is the single source of truth for the version number. `installer.iss` must be manually updated to match for releases.
-   **Linting/Formatting:** The project follows standard Python practices; use `pytest` for all functional verification.
