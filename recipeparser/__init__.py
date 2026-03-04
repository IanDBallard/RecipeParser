"""
recipeparser — extract recipes from EPUB cookbooks and export to Paprika 3.

Public API
----------
    from recipeparser import process_epub
    process_epub("path/to/book.epub", "./output")
"""
import os

from dotenv import load_dotenv
from google import genai

from recipeparser.paths import get_default_output_dir, get_env_file

# Load from user app data first; project .env overrides (for dev)
load_dotenv(get_env_file())
load_dotenv()

_api_key = os.environ.get("GOOGLE_API_KEY", "")
client = genai.Client(api_key=_api_key) if _api_key else None


def process_epub(
    epub_path: str,
    output_dir: str | None = None,
    units: str = "book",
    concurrency: int | None = None,
    rpm: int | None = None,
):
    """Convenience wrapper that uses the module-level Gemini client.

    ``units``: "metric" | "us" | "imperial" | "book" (default).
    ``concurrency``: max in-flight API calls (1–10, default 1).
    ``rpm``: optional requests-per-minute limit; when set, constrains starts per 60s window.
    """
    if client is None:
        from recipeparser.exceptions import ConfigurationError
        raise ConfigurationError(
            "GOOGLE_API_KEY is not set.  Add it to your .env file or environment."
        )
    from recipeparser.pipeline import process_epub as _run

    if output_dir is None:
        output_dir = str(get_default_output_dir())
    return _run(
        epub_path, output_dir, client,
        units=units, concurrency=concurrency, rpm=rpm,
    )
