"""
recipeparser/core/stages/embed.py — EMBED stage.

Wraps gemini.get_embeddings() to generate a 1536-dimension vector for a
refined recipe.  The embedding text is built by build_embedding_text() from the recipe title,
category names, ingredient fallback strings and the first DIRECTIONS_CHARS
characters of the directions (Cayenne AI search §6).

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import Any, List, Sequence

from recipeparser.core.fat_tokens import strip_fat_tokens
from recipeparser.gemini import get_embeddings
from recipeparser.models import CayenneRefinement

log = logging.getLogger(__name__)

_EXPECTED_DIMS = 1536
DIRECTIONS_CHARS = 400


def build_embedding_text(
    title: str,
    category_names: Sequence[str],
    ingredient_lines: Sequence[str],
    directions: Sequence[str],
) -> str:
    """One line per part, empty parts omitted: title, categories, ingredients, directions."""
    names = [n.strip() for n in category_names if n and n.strip()]
    ingredients = " ".join(i.strip() for i in ingredient_lines if i and i.strip())
    steps = " ".join(d.strip() for d in directions if d and d.strip())
    parts = [
        title.strip(),
        ", ".join(names),
        ingredients,
        steps[:DIRECTIONS_CHARS].strip(),
    ]
    return "\n".join(p for p in parts if p)


def embed(
    recipe: CayenneRefinement,
    client: Any,
    *,
    category_names: Sequence[str] = (),
) -> List[float]:
    """
    Generate a 1536-dimension embedding vector for a refined recipe.

    The embedding input is build_embedding_text(): title, category names,
    ingredient fallback strings, then the start of the directions with Fat
    Tokens replaced by their words.  Callers that do not know the categories
    pass none; their vector is interim and the database triggers queue the
    full one.

    Args:
        recipe:  A ``CayenneRefinement`` produced by the REFINE stage.
        client:  An initialised ``google.genai.Client`` instance.
        category_names:  The recipe's category names, if known.

    Returns:
        A ``List[float]`` of length 1536.

    Raises:
        RuntimeError: If the Gemini embedding call fails or returns a vector
                      of unexpected dimensionality.
    """
    embedding_input = build_embedding_text(
        recipe.title,
        category_names,
        [i.fallback_string for i in recipe.structured_ingredients],
        [strip_fat_tokens(d.text) for d in recipe.tokenized_directions],
    )

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
