"""
recipeparser/core/stages/assemble.py — ASSEMBLE stage.

Pure function that combines the outputs of REFINE, EMBED, and CATEGORIZE
into a final IngestResponse.  Makes zero API calls.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import Dict, List, Optional

from recipeparser.core.citation import Citation
from recipeparser.core.durations import duration_columns
from recipeparser.core.models import SourceMeta
from recipeparser.core.regen import raw_lines_from_derived
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
    meta: Optional[SourceMeta] = None,
    ingredient_lines: Optional[List[str]] = None,
    direction_steps: Optional[List[str]] = None,
    servings_text: Optional[str] = None,
    citation: Optional[Citation] = None,
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
        meta:            Fields the source stated for itself (Paprika only). A value
                         here wins over the extracted one; a field absent from meta
                         falls back to the argument rather than blanking it.
        ingredient_lines: Raw ingredient lines from EXTRACT. When None or empty,
                          derived from the refinement's fallback strings.
        direction_steps:  Raw direction steps from EXTRACT. When None or empty,
                          derived from the tokenized text with tokens stripped.
        servings_text:    The extracted servings string ("4", "2-4"). Parsed into
                          servings_min/max/note; servings_min becomes base_servings.
        citation:         What the reader and the model settled about the source
                          (core.citation.resolve_citation). Fills the four
                          citation columns and, when nothing else supplies
                          `source`, its display form.

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

    # The source's own statement beats the extractor's reading of it. A field the
    # source left blank falls through to the extracted value rather than erasing it:
    # a Paprika entry with only a cook time must not cost us the prep time Gemini
    # found in the directions.
    if meta is not None:
        prep_time = meta.prep_time or prep_time
        cook_time = meta.cook_time or cook_time

    derived_lines, derived_steps = raw_lines_from_derived(
        recipe.structured_ingredients, recipe.tokenized_directions
    )
    lines = list(ingredient_lines) if ingredient_lines else derived_lines
    steps = list(direction_steps) if direction_steps else derived_steps
    cols = duration_columns(prep_time, cook_time, servings_text, recipe.base_servings)

    result = IngestResponse(
        title=recipe.title,
        prep_time=prep_time,
        cook_time=cook_time,
        base_servings=cols["base_servings"],
        source_url=source_url,
        image_url=image_url,
        categories=flat_categories,
        grid_categories=grid_categories,
        structured_ingredients=recipe.structured_ingredients,
        tokenized_directions=recipe.tokenized_directions,
        embedding=embedding,
        # No extracted fallback: only a Paprika entry states these, and nothing
        # infers them from a book or a web page.
        # Paprika's own statement first; else the citation's display form, so the
        # library row (which reads `source`) shows the same thing for a fresh
        # insert as for a backfilled row; else null.
        source=(meta.source if meta else None) or (citation.display() if citation else None),
        source_kind=citation.kind if citation else None,
        source_key=citation.key if citation else None,
        source_title=citation.title if citation else None,
        source_author=citation.author if citation else None,
        notes=meta.notes if meta else None,
        rating=meta.rating if meta else None,
        nutritional_info=meta.nutritional_info if meta else None,
        description=meta.description if meta else None,
        difficulty=meta.difficulty if meta else None,
        ingredient_lines=lines,
        direction_steps=steps,
        prep_min_minutes=cols["prep_min_minutes"],
        prep_max_minutes=cols["prep_max_minutes"],
        prep_note=cols["prep_note"],
        cook_min_minutes=cols["cook_min_minutes"],
        cook_max_minutes=cols["cook_max_minutes"],
        cook_note=cols["cook_note"],
        servings_min=cols["servings_min"],
        servings_max=cols["servings_max"],
        servings_note=cols["servings_note"],
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
