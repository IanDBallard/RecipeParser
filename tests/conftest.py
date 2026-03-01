"""Shared fixtures and helpers for the recipeparser test suite."""
import os
from unittest.mock import MagicMock

# Ensure a dummy API key exists so __init__.py can construct the client
# without a real .env file present.
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")

from recipeparser.models import RecipeExtraction  # noqa: E402 (env must be set first)


def make_recipe(name: str, photo: str | None = None) -> RecipeExtraction:
    return RecipeExtraction(
        name=name,
        photo_filename=photo,
        ingredients=["1 cup flour", "1/2 tsp salt"],
        directions=["Mix ingredients.", "Bake at 350F for 30 mins."],
    )


def make_mock_client(return_value=None, side_effect=None):
    """Return a minimal mock of google.genai.Client with generate_content configured."""
    client = MagicMock()
    if side_effect is not None:
        client.models.generate_content.side_effect = side_effect
    else:
        client.models.generate_content.return_value = return_value
    return client
