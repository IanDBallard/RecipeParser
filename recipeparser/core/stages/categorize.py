"""
recipeparser/core/stages/categorize.py — CATEGORIZE stage.

Extracts grid_categories from a CayenneRefinement.  This is NOT a separate
Gemini call — categorization is performed inside the REFINE stage (Pass 2)
as part of refine_recipe_for_cayenne().  This stage simply reads the result
and filters it against the user's defined axes.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import Dict, List, Sequence, Set, TypeVar

from recipeparser.models import CayenneRefinement

log = logging.getLogger(__name__)

_T = TypeVar("_T")


def chunked(items: Sequence[_T], size: int) -> List[List[_T]]:
    """Split items into consecutive lists of at most ``size`` (spec 6.2 batches of 10)."""
    size = max(1, size)
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def filter_batch_result(result: Dict[str, List[str]], offered: Set[str]) -> Dict[str, List[str]]:
    """Keep only offered tags, deduplicated, and drop recipes with no matches."""
    clean: Dict[str, List[str]] = {}
    for recipe_id, tags in result.items():
        kept: List[str] = []
        for tag in tags:
            if tag in offered and tag not in kept:
                kept.append(tag)
        if kept:
            clean[recipe_id] = kept
    return clean


def categorize(
    recipe: CayenneRefinement,
    user_axes: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """
    Extract and validate the grid_categories from a refined recipe.

    The actual categorization was performed by Gemini inside the REFINE stage.
    This function reads ``recipe.grid_categories``, filters out any tags that
    are not in the user's defined axes (defensive guard against hallucination),
    and returns the clean result.

    Args:
        recipe:     A ``CayenneRefinement`` produced by the REFINE stage.
        user_axes:  Dict mapping axis name → list of valid tag strings.
                    e.g. {"Cuisine": ["Italian", "Mexican"], "Protein": ["Chicken"]}
                    When empty, returns {} immediately (no-op).

    Returns:
        A ``Dict[str, List[str]]`` of axis → selected tags.
        Returns ``{}`` if ``user_axes`` is empty or no categories were assigned.
    """
    if not user_axes:
        log.debug("categorize(): user_axes is empty — skipping categorization.")
        return {}

    raw_grid = recipe.grid_categories or {}

    clean: Dict[str, List[str]] = {}
    for axis_name, valid_tags in user_axes.items():
        selected = raw_grid.get(axis_name, [])
        valid_set = set(valid_tags)
        filtered = [t for t in selected if t in valid_set]
        if filtered:
            clean[axis_name] = filtered

    log.info(
        "categorize(): %d axis/axes assigned from %d available.",
        len(clean),
        len(user_axes),
    )
    return clean
