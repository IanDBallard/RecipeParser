"""
recipeparser/core/stages/assemble.py — ASSEMBLE stage.

Pure function that combines the outputs of REFINE, EMBED, and CATEGORIZE
into a final IngestResponse.  Makes zero API calls.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import Dict, List, Optional

from recipeparser.models import CayenneRefinement, IngestResponse

log = logging.getLogger(__name__)


def assemble(
    recipe: CayenneRefinement,
    embedding: List[float],
    source_url: Optional[str],
    image_url: Optional[str],
    grid_categories: Dict[str, List[str]],
    prep_time: Optional[str] = None,
    cook_time: Optional[str] = None,
) -> IngestResponse:
    """
    Assemble the final IngestResponse from stage outputs.

    This is a pure function — it performs no API calls and has no side effects.
    It combines the structured recipe data, embedding vector, and category
    assignments into the canonical output shape that writers (Supabase, ZIP,
    etc.) consume.

    The ``categories`` field on IngestResponse is populated as a flat list of
    tag strings derived from ``grid_categories`` values, for backward
    compatibility with consumers that expect a simple list.

    Args:
        recipe:          A ``CayenneRefinement`` from the REFINE stage.
        embedding:       A 1536-dim float list from the EMBED stage.
        source_url:      The original source URL (or None for file/text input).
        image_url:       The Supabase Storage URL of the hero image (or None).
        grid_categories: The validated axis→tags dict from the CATEGORIZE stage.
        prep_time:       Prep time string from the EXTRACT stage (or None).
        cook_time:       Cook time string from the EXTRACT stage (or None).

    Returns:
        A fully-populated ``IngestResponse`` ready for persistence.
    """
    # Flatten grid_categories into a simple list of tag strings for the
    # legacy `categories` field (e.g. ["Italian", "Chicken"])
    flat_categories: List[str] = [
        tag
        for tags in grid_categories.values()
        for tag in tags
    ]

    result = IngestResponse(
        title=recipe.title,
        prep_time=prep_time,
        cook_time=cook_time,
        base_servings=recipe.base_servings,
        source_url=source_url,
        image_url=image_url,
        categories=flat_categories,
        grid_categories=grid_categories,
        structured_ingredients=recipe.structured_ingredients,
        tokenized_directions=recipe.tokenized_directions,
        embedding=embedding,
    )

    log.info(
        "assemble(): '%s' → %d ingredients, %d steps, %d categories, %d-dim embedding.",
        result.title,
        len(result.structured_ingredients),
        len(result.tokenized_directions),
        len(result.categories),
        len(result.embedding),
    )
    return result
