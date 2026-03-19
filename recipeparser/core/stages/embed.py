"""
recipeparser/core/stages/embed.py — EMBED stage.

Wraps gemini.get_embeddings() to generate a 1536-dimension vector for a
refined recipe.  The embedding text is constructed from the recipe title
and all ingredient fallback strings.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import List

from recipeparser.gemini import get_embeddings
from recipeparser.models import CayenneRefinement

log = logging.getLogger(__name__)

_EXPECTED_DIMS = 1536


def embed(
    recipe: CayenneRefinement,
    client,
) -> List[float]:
    """
    Generate a 1536-dimension embedding vector for a refined recipe.

    The embedding input is constructed as:
        "<title>\\n<fallback_string_1> <fallback_string_2> ..."

    This matches the format used by the existing pipeline so that embeddings
    are comparable across old and new code paths.

    Args:
        recipe:  A ``CayenneRefinement`` produced by the REFINE stage.
        client:  An initialised ``google.genai.Client`` instance.

    Returns:
        A ``List[float]`` of length 1536.

    Raises:
        RuntimeError: If the Gemini embedding call fails or returns a vector
                      of unexpected dimensionality.
    """
    ingredient_text = " ".join(
        ing.fallback_string for ing in recipe.structured_ingredients
    )
    embedding_input = f"{recipe.title}\n{ingredient_text}".strip()

    try:
        vector = get_embeddings(embedding_input, client)
    except Exception as exc:
        raise RuntimeError(
            f"embed(): Gemini embedding call failed for '{recipe.title}': {exc}"
        ) from exc

    if len(vector) != _EXPECTED_DIMS:
        raise RuntimeError(
            f"embed(): expected {_EXPECTED_DIMS}-dim vector, got {len(vector)} "
            f"for recipe '{recipe.title}'."
        )

    log.info("embed(): generated %d-dim vector for '%s'.", len(vector), recipe.title)
    return vector
