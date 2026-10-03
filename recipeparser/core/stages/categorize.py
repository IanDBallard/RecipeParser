"""
recipeparser/core/stages/categorize.py — CATEGORIZE stage.

Extracts grid_categories from a CayenneRefinement.  This is NOT a separate
Gemini call — categorization is performed inside the REFINE stage (Pass 2)
as part of refine_recipe_for_cayenne().  This stage simply reads the result
and filters it against the user's defined axes.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import Dict, List, Mapping, Optional, Sequence, Set, TypeVar

from recipeparser.core.taxonomy import most_specific
from recipeparser.models import CayenneRefinement

log = logging.getLogger(__name__)

#: The most tags one axis may give a recipe.  The prompts ask for one and allow
#: two; this holds it whatever the model returns (Cayenne Fix Roadmap F-205).
MAX_TAGS_PER_AXIS = 2

_T = TypeVar("_T")


def chunked(items: Sequence[_T], size: int) -> List[List[_T]]:
    """Split items into consecutive lists of at most ``size`` (spec 6.2 batches of 10)."""
    size = max(1, size)
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def axis_tags(
    selected: Sequence[str],
    valid: Sequence[str],
    parents: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """
    One axis's answer made safe to write: offered tags only, deduplicated, no tag
    beside its own descendant, at most ``MAX_TAGS_PER_AXIS``, in the model's order.

    Pruning comes before the cap, so a parent picked beside its child does not
    take the place of a second real tag.
    """
    valid_set = set(valid)
    kept = most_specific([t for t in selected if t in valid_set], parents or {})
    return kept[:MAX_TAGS_PER_AXIS]


def filter_batch_result(
    result: Dict[str, List[str]],
    offered: Set[str],
    axes: Optional[Mapping[str, Sequence[str]]] = None,
    parents: Optional[Mapping[str, str]] = None,
) -> Dict[str, List[str]]:
    """
    Keep only offered tags, deduplicated, and drop recipes with no matches.

    With ``axes`` (axis → the tags this job offers on it), each recipe's tags
    are also held to ``axis_tags`` axis by axis, as an ingest's are.  The batch
    answer is one flat list per recipe, so the axes have to be supplied.
    """
    clean: Dict[str, List[str]] = {}
    for recipe_id, tags in result.items():
        kept: List[str] = []
        for tag in tags:
            if tag in offered and tag not in kept:
                kept.append(tag)
        if axes is not None:
            kept = [t for valid in axes.values() for t in axis_tags(kept, valid, parents)]
        if kept:
            clean[recipe_id] = kept
    return clean


def categorize(
    recipe: CayenneRefinement,
    user_axes: Dict[str, List[str]],
    parents: Optional[Mapping[str, str]] = None,
) -> Dict[str, List[str]]:
    """
    Extract and validate the grid_categories from a refined recipe.

    The actual categorization was performed by Gemini inside the REFINE stage.
    This function reads ``recipe.grid_categories``, filters out any tags that
    are not in the user's defined axes (defensive guard against hallucination),
    holds each axis to ``axis_tags`` (no tag beside its own descendant, at most
    ``MAX_TAGS_PER_AXIS``), and returns the clean result.

    Args:
        recipe:     A ``CayenneRefinement`` produced by the REFINE stage.
        user_axes:  Dict mapping axis name → list of valid tag strings.
                    e.g. {"Cuisine": ["Italian", "Mexican"], "Protein": ["Chicken"]}
                    When empty, returns {} immediately (no-op).
        parents:    ``{tag: parent tag}`` for nested tags (``CategorySource.load_parents``).
                    None or empty prunes nothing.

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
        filtered = axis_tags(raw_grid.get(axis_name, []), valid_tags, parents)
        if filtered:
            clean[axis_name] = filtered

    log.info(
        "categorize(): %d axis/axes assigned from %d available.",
        len(clean),
        len(user_axes),
    )
    return clean
