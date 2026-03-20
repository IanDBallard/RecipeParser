"""
recipeparser/core/stages/refine.py — REFINE stage.

Wraps gemini.refine_recipe_for_cayenne() to convert a raw RecipeExtraction
into a structured CayenneRefinement (Fat Tokens + UOM conversion +
categorization in one Gemini call).

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from recipeparser.gemini import refine_recipe_for_cayenne
from recipeparser.models import CayenneRefinement, RecipeExtraction

log = logging.getLogger(__name__)

# Fat Token regex — must match {{ing_01|fallback text}}
_FAT_TOKEN_RE = re.compile(r"\{\{([^|]+)\|([^}]+)\}\}")


def _validate_fat_tokens(refinement: CayenneRefinement) -> None:
    """
    Verify that every Fat Token in tokenized_directions references a valid
    ingredient ID.  Raises ValueError on the first violation found.
    """
    valid_ids = {ing.id for ing in refinement.structured_ingredients}
    for step in refinement.tokenized_directions:
        for match in _FAT_TOKEN_RE.finditer(step.text):
            token_id = match.group(1)
            if token_id not in valid_ids:
                raise ValueError(
                    "refine(): Fat Token '{{" + token_id + "|...}}' in step "
                    + str(step.step)
                    + f" references unknown ingredient ID '{token_id}'. "
                    + f"Valid IDs: {sorted(valid_ids)}"
                )


def refine(
    raw: RecipeExtraction,
    client: Any,
    *,
    uom_system: str = "US",
    measure_preference: str = "Volume",
    user_axes: Optional[Dict[str, List[str]]] = None,
) -> CayenneRefinement:
    """
    Refine a raw RecipeExtraction into a structured CayenneRefinement.

    This is Pass 2 of the pipeline.  A single Gemini call handles:
      - Structured ingredient parsing (id, amount, unit, name, fallback_string)
      - Fat Token injection into direction text
      - Optional Volume-to-Weight UOM conversion (flagged as is_ai_converted)
      - Multipolar categorization via grid_categories (when user_axes provided)

    Args:
        raw:               The RecipeExtraction from the EXTRACT stage.
        client:            An initialised ``google.genai.Client`` instance.
        uom_system:        "US", "Metric", or "Imperial".
        measure_preference: "Volume" or "Weight".
        user_axes:         Optional dict of axis_name → [tag, ...].
                           When None or empty, grid_categories will be {} in
                           the result.

    Returns:
        A validated ``CayenneRefinement`` object.

    Raises:
        ValueError: If Gemini returns None, or if Fat Token validation fails.
    """
    result = refine_recipe_for_cayenne(
        raw_recipe=raw,
        client=client,
        uom_system=uom_system,
        measure_preference=measure_preference,
        user_axes=user_axes,
    )

    if result is None:
        raise ValueError(
            f"refine(): Gemini returned None for recipe '{getattr(raw, 'name', '?')}'. "
            "The refinement call failed — check logs for details."
        )

    _validate_fat_tokens(result)
    log.info(
        "refine(): '%s' → %d ingredients, %d steps.",
        result.title,
        len(result.structured_ingredients),
        len(result.tokenized_directions),
    )
    return result
