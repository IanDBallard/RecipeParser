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
):
    """Convenience wrapper that uses the module-level Gemini client.

    ``units`` controls how dual-measurement ingredient lines are handled:
      "metric"   — keep only gram/ml values
      "us"       — keep only US cup/tbsp values
      "imperial" — keep only oz/lb values
      "book"     — preserve whatever the book uses (default)
    """
    if client is None:
        from recipeparser.exceptions import ConfigurationError
        raise ConfigurationError(
            "GOOGLE_API_KEY is not set.  Add it to your .env file or environment."
        )
    from recipeparser.pipeline import process_epub as _run

    if output_dir is None:
        output_dir = str(get_default_output_dir())
    return _run(epub_path, output_dir, client, units=units)
