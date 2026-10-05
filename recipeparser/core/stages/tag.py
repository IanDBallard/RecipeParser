"""
recipeparser/core/stages/tag.py — TAG stage (Cayenne Fix Roadmap F-246).

Tags finished recipes one axis per Gemini call, ten recipes per call: the shape
``scripts/retag_axis.py`` uses, which scored 105/105 on the 14-recipe sample on
2026-10-03 where REFINE, offered every axis in one request, tagged potato gnocchi
and a Victoria sponge ``Egg`` in every run. The variable is how many axes one
request offers. REFINE no longer categorises; the pipeline queues each finished
recipe and runs this over every ten (design
``docs/superpowers/specs/2026-10-05-per-axis-tagging-at-import-design.md`` in
the Cayenne repository).

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from recipeparser.core.stages.categorize import filter_batch_result
from recipeparser.models import IngestResponse

log = logging.getLogger(__name__)

#: Recipes per call, as the retag and the bulk recategorise batch them (spec 6.2).
TAG_BATCH_SIZE = 10

#: ``(rows, {axis: tags}) -> {row id: tags}``; ``gemini.categorize_batch`` with
#: its client, parents and limiter bound by the caller.
CategorizeFn = Callable[[List[Dict[str, Any]], Dict[str, List[str]]], Dict[str, List[str]]]


@dataclass(frozen=True)
class TagFailure:
    """One axis that could not be asked for one batch: those recipes keep no tags on it."""
    axis: str
    titles: List[str]
    reason: str


def _rows(recipes: Sequence[IngestResponse]) -> List[Dict[str, Any]]:
    """The recipes as the categorise prompt reads them: title and the verbatim lines (design D7)."""
    return [
        {
            "id": f"recipe-{i + 1}",
            "title": r.title,
            "ingredient_lines": list(r.ingredient_lines),
            "direction_steps": list(r.direction_steps),
        }
        for i, r in enumerate(recipes)
    ]


def tag_batch(
    recipes: Sequence[IngestResponse],
    user_axes: Mapping[str, Sequence[str]],
    parents: Optional[Mapping[str, str]],
    categorize: CategorizeFn,
) -> List[TagFailure]:
    """
    Ask for every axis over ``recipes``, one axis per call, and write each recipe's
    answer onto it as ``grid_categories`` (axis order) and the flat ``categories``.

    An axis whose call fails twice leaves that axis empty for these recipes and is
    returned as a ``TagFailure``; the other axes are unaffected (design D8). Each
    answer is held to ``filter_batch_result``: offered tags only, no tag beside its
    own descendant, at most two per axis.
    """
    rows = _rows(recipes)
    grids: List[Dict[str, List[str]]] = [{} for _ in recipes]
    failures: List[TagFailure] = []
    for axis, tags in user_axes.items():
        offered = list(tags)
        if not offered:
            continue
        hits: Optional[Dict[str, List[str]]] = None
        reason = ""
        for _attempt in (1, 2):
            try:
                raw = categorize(rows, {axis: offered})
                hits = filter_batch_result(raw, set(offered), {axis: offered}, parents)
                break
            except Exception as exc:  # noqa: BLE001 -- one axis must not cost the others
                reason = str(exc)[:300]
                log.warning("TAG: the %r call failed for %d recipe(s): %s", axis, len(recipes), reason)
        if hits is None:
            failures.append(TagFailure(axis, [r.title for r in recipes], reason))
            continue
        for i, row in enumerate(rows):
            if hits.get(row["id"]):
                grids[i][axis] = hits[row["id"]]
    for recipe, grid in zip(recipes, grids):
        recipe.grid_categories = grid
        recipe.categories = [t for tags in grid.values() for t in tags]
    log.info("TAG: %d recipe(s) across %d axis/axes, %d failed.", len(recipes), len(user_axes), len(failures))
    return failures
