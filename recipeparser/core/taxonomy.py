"""
recipeparser/core/taxonomy.py — the one walk from a category to its axis.

Ingest and bulk recategorise both have to describe the user's taxonomy to the
model, and until 2026-09-13 they described it differently.  ``_build_axes``
walked from each root down, flattening every descendant into that root's axis;
``resolve_new_axes`` read a node's immediate ``parent_id`` instead.  On a
three-deep tree (``Cuisine > Asian > Thai``) ingest offered Thai under
*Cuisine* and recategorise offered it under *Asian* — the same tag, two axes,
and no test that went deeper than two levels to notice.  A prompt that
disagrees with itself between the two paths misfiles recipes quietly, so the
walk lives here once and both callers use it.

Pure: flat ``{id, name, parent_id}`` row dicts in, plain structures out.  No
Supabase, no I/O, no logging.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Set


def _index(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    return {r["id"]: r for r in rows if r.get("id")}


def root_of(category_id: str, rows: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """
    The id of the top-level ancestor of ``category_id``, or the id itself when
    it is already top-level.  None when the id is not among ``rows``.

    Cycle-safe: a parent chain that loops (only reachable through a cross-device
    race, and surfaced as an invalid hierarchy on the client) terminates at the
    first repeat rather than spinning.  Returning the last id seen is the same
    answer the client's tree builder settles on when it gives up on a chain.
    """
    by_id = _index(rows)
    if category_id not in by_id:
        return None
    seen: Set[str] = set()
    current = category_id
    while True:
        if current in seen:
            return current
        seen.add(current)
        parent = by_id.get(current, {}).get("parent_id")
        if not parent or parent not in by_id:
            return current
        current = parent


def axes_from_rows(rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """
    ``{axis name: [tag names]}`` for a whole taxonomy.

    A top-level row is an axis.  Every descendant of it, at any depth, is a tag
    of that axis — the tree is flattened, not preserved, because the model is
    asked to pick tags and not to navigate.  A childless axis becomes a
    single-tag axis of its own name, so it can still be matched against.

    Rows with no id or a blank name are skipped, as they always were.
    """
    id_to_name: Dict[str, str] = {}
    children: Dict[str, List[str]] = {}
    top_level: List[str] = []

    for row in rows:
        cat_id = row.get("id") or ""
        name = (row.get("name") or "").strip()
        if not cat_id or not name:
            continue
        id_to_name[cat_id] = name
        parent_id = row.get("parent_id")
        if parent_id:
            children.setdefault(parent_id, []).append(cat_id)
        else:
            top_level.append(cat_id)

    axes: Dict[str, List[str]] = {}
    for axis_id in top_level:
        axis_name = id_to_name[axis_id]
        tags: List[str] = []
        queue = list(children.get(axis_id, []))
        visited: Set[str] = set()
        while queue:
            child_id = queue.pop(0)
            if child_id in visited:
                continue
            visited.add(child_id)
            child_name = id_to_name.get(child_id)
            if child_name:
                tags.append(child_name)
            queue.extend(children.get(child_id, []))
        axes[axis_name] = sorted(tags) if tags else [axis_name]
    return axes


def descendants_of(category_id: str, rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """
    ``category_id`` plus every id beneath it, de-duplicated, in breadth-first
    order.  This is D4's "a folder offers its subtree": the endpoint expands
    what the caller asked for before the job carries it, so the client never
    decides how far a request reaches.

    An unknown id yields an empty list, which is what lets the endpoint tell
    "not yours" from "has no children".
    """
    by_id = _index(rows)
    if category_id not in by_id:
        return []
    children: Dict[str, List[str]] = {}
    for row in rows:
        parent_id = row.get("parent_id")
        if parent_id and row.get("id"):
            children.setdefault(parent_id, []).append(row["id"])
    out: List[str] = []
    seen: Set[str] = set()
    queue = [category_id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        out.append(current)
        queue.extend(children.get(current, []))
    return out


def parents_from_rows(rows: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """
    ``{tag name: parent tag name}`` for every tag nested below another tag.

    A tag directly under its axis has no entry: the axis is not a tag the model
    can choose beside it (``axes_from_rows`` offers an axis only when it has no
    children).  The flattened axes hide the tree from the model, and this is
    what puts it back: the prompts show ``Thai`` as under ``Asian``, and
    ``most_specific`` drops ``Asian`` when the model picks both, since the
    library filter already finds Thai under Asian (Cayenne Fix Roadmap F-205).

    Names identify categories: the unique ``(user_id, name)`` index makes a
    duplicate unrepresentable (D6).
    """
    by_id = _index(rows)
    parents: Dict[str, str] = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        parent = by_id.get(row.get("parent_id") or "")
        if not name or parent is None or not parent.get("parent_id"):
            continue
        parent_name = (parent.get("name") or "").strip()
        if parent_name:
            parents[name] = parent_name
    return parents


def most_specific(tags: Sequence[str], parents: Mapping[str, str]) -> List[str]:
    """
    ``tags`` without any tag that is an ancestor of another tag in it, order kept.

    Cycle-safe: a parent chain that loops (a cross-device race the client
    reports as an invalid hierarchy) stops at the first repeat, and two tags
    on one loop are each other's ancestor, so neither is dropped.
    """
    chosen = list(dict.fromkeys(tags))

    def chain(tag: str) -> Set[str]:
        seen: Set[str] = set()
        current = parents.get(tag)
        while current and current not in seen:
            seen.add(current)
            current = parents.get(current)
        return seen

    above = {tag: chain(tag) for tag in chosen}
    # Strictly above: on a loop two tags are each other's ancestor, and neither is dropped.
    return [t for t in chosen if not any(t in above[u] and u not in above[t] for u in chosen if u != t)]
