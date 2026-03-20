"""
recipeparser — AI-powered recipe extraction and ingestion for Project Cayenne.

Entry points
------------
  CLI adapter:  python -m recipeparser  (or `recipeparser` console script)
  API adapter:  uvicorn recipeparser.adapters.api:app
  GUI adapter:  python -m recipeparser --gui
"""
import os

from dotenv import load_dotenv
from google import genai

from recipeparser.paths import get_env_file

# Load from user app data first; project .env overrides (for dev)
load_dotenv(get_env_file())
load_dotenv()

_api_key = os.environ.get("GOOGLE_API_KEY", "")
client = genai.Client(api_key=_api_key) if _api_key else None
