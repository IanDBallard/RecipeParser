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


def process_epub(epub_path: str, output_dir: str = "./output"):
    """Convenience wrapper that uses the module-level Gemini client."""
    if client is None:
        raise EnvironmentError(
            "GOOGLE_API_KEY is not set.  Add it to your .env file or environment."
        )
    from recipeparser.pipeline import process_epub as _run

    return _run(epub_path, output_dir, client)
