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


def _normalise_line_index(refinement: CayenneRefinement, raw: RecipeExtraction) -> None:
    """
    Clear any ``line_index`` that does not point at a distinct line of
    ``raw.ingredients``, in place.  Valid indices are left untouched.

    ``line_index`` is optional by design: the model is ``Optional[int]`` and
    spec 4.3 defines the client-side fallback for a missing or wrong index (the
    client compares ``fallback_string`` against the line and bumps ``body_rev``
    instead of writing an override).  An out-of-range or duplicated index is a
    plausible model slip on a long sectioned ingredient list, and raising here
    propagates to the per-chunk error boundary and drops the whole recipe at
    ingest — destroying a recipe over a cosmetic error in a field whose absence
    the design already tolerates.  Degrade the offending entries to ``None`` and
    log which ones, so the fallback path handles them.
    """
    n_lines = len(raw.ingredients)
    seen: Dict[int, str] = {}
    dropped: List[str] = []
    for ing in refinement.structured_ingredients:
        idx = ing.line_index
        if idx is None:
            continue
        if idx < 0 or idx >= n_lines:
            dropped.append(f"'{ing.id}' (line_index {idx}, {n_lines} raw line(s))")
            ing.line_index = None
        elif idx in seen:
            dropped.append(f"'{ing.id}' (line_index {idx} already claimed by '{seen[idx]}')")
            ing.line_index = None
        else:
            seen[idx] = ing.id
    if dropped:
        log.warning(
            "refine(): '%s' — dropped %d unusable line_index value(s); the client falls "
            "back to fallback_string matching for these (spec 4.3): %s",
            getattr(raw, "name", "?"), len(dropped), ", ".join(dropped),
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

    Note:
        An unusable ``line_index`` (out of range, or claimed twice) is *not* an
        error: the offending entries are set to ``None`` with a warning and the
        recipe is kept.  The field is optional and spec 4.3 defines the
        client-side fallback for it.
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
    _normalise_line_index(result, raw)
    log.info(
        "refine(): '%s' → %d ingredients, %d steps.",
        result.title,
        len(result.structured_ingredients),
        len(result.tokenized_directions),
    )
    return result
