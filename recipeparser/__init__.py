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

load_dotenv()

_api_key = os.environ.get("GOOGLE_API_KEY", "")
client = genai.Client(api_key=_api_key) if _api_key else None


def process_epub(epub_path: str, output_dir: str = "./output", units: str = "book"):
    """Convenience wrapper that uses the module-level Gemini client.

    ``units`` controls how dual-measurement ingredient lines are handled:
      "metric"   — keep only gram/ml values
      "us"       — keep only US cup/tbsp values
      "imperial" — keep only oz/lb values
      "book"     — preserve whatever the book uses (default)
    """
    if client is None:
        raise EnvironmentError(
            "GOOGLE_API_KEY is not set.  Add it to your .env file or environment."
        )
    from recipeparser.pipeline import process_epub as _run

    return _run(epub_path, output_dir, client, units=units)
