"""
recipeparser/core/stages/categorize.py — the rules every tagging answer is held to.

``axis_tags`` and ``filter_batch_result`` make a model's answer safe to write, for
the import's TAG stage (``core/stages/tag.py``), the bulk recategorise and the
retag script alike. Until Cayenne Fix Roadmap F-246 a ``categorize()`` here read
REFINE's ``grid_categories``; REFINE no longer categorises.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import Dict, List, Mapping, Optional, Sequence, Set, TypeVar

from recipeparser.core.taxonomy import most_specific

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
