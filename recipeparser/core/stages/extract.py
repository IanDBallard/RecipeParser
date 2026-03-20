"""
recipeparser/core/stages/extract.py — EXTRACT stage.

Wraps gemini.extract_recipes() / gemini.extract_recipe_from_text() into a
single clean interface.  Returns a list of RecipeExtraction objects; returns
[] for chunks that contain no recipes (not an error).

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import Any, List

from recipeparser.gemini import (
    extract_recipe_from_text,
    extract_recipes,
    needs_table_normalisation,
    normalise_baker_table,
)
from recipeparser.models import RecipeExtraction

log = logging.getLogger(__name__)


def extract(
    chunk_text: str,
    client: Any,
    *,
    units: str = "book",
    plain_text_mode: bool = False,
) -> List[RecipeExtraction]:
    """
    Extract all recipes from a single text chunk.

    Args:
        chunk_text:      The raw text to process.  Must be non-empty.
        client:          An initialised ``google.genai.Client`` instance.
        units:           UOM preference passed to ``extract_recipes``.
                         One of "book" | "metric" | "us" | "imperial".
                         Ignored when ``plain_text_mode`` is True.
        plain_text_mode: When True, uses the simpler ``extract_recipe_from_text``
                         prompt (suited for pasted/Paprika text rather than
                         EPUB/PDF book chunks).

    Returns:
        A list of ``RecipeExtraction`` objects.  Returns ``[]`` when the chunk
        contains no recognisable recipes — this is NOT an error condition.

    Raises:
        ValueError: If ``chunk_text`` is empty or whitespace-only.
    """
    if not chunk_text or not chunk_text.strip():
        raise ValueError("extract(): chunk_text must be non-empty.")

    # Baker's-percentage table pre-processing (book chunks only)
    if not plain_text_mode and needs_table_normalisation(chunk_text):
        log.info("extract(): baker's-percentage table detected — normalising.")
        chunk_text = normalise_baker_table(chunk_text, client)

    if plain_text_mode:
        result = extract_recipe_from_text(chunk_text, client)
    else:
        result = extract_recipes(chunk_text, client, units=units)

    if result is None:
        log.warning("extract(): Gemini returned None — treating as empty chunk.")
        return []

    recipes: List[RecipeExtraction] = result.recipes if result.recipes else []
    log.info("extract(): found %d recipe(s) in chunk.", len(recipes))
    return recipes
