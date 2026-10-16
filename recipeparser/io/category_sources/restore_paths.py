"""restore_paths.py -- create the categories a Cayenne restore needs, level by level.

A Cayenne-authored .paprikarecipes entry lists each category as a path from the root
(``["Course", "Dessert", "Cake"]``). A fresh account lacks those nodes, so before the
recipes are written the missing ones are created here and the writer then links by name.

Rules (the starter-category trigger in Cayenne works the same way):
  * A name that already exists anywhere in the account is reused, never moved: the
    unique index is (user_id, name), so a name identifies one category.
  * A level is inserted in one request with ``on_conflict=user_id,name`` and
    ``resolution=ignore-duplicates``, so a concurrent job's insert is not an error;
    the next level re-reads to learn the ids that actually exist.
  * Depth is capped at 4 (root = 1); a node that would be deeper is not created.
"""

import logging
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from recipeparser.config import live_writes_blocked

log = logging.getLogger(__name__)

MAX_DEPTH = 4


def restore_grid(placed: Iterable[str]) -> Dict[str, List[str]]:
    """The ``grid_categories`` a restored recipe is linked through: one axis, its placed names."""
    return {"restore": sorted(set(placed))}


def _read(user_id: str, supabase_url: str, service_key: str) -> Dict[str, Dict[str, Any]]:
    """The account's categories keyed by name: ``{"id", "name", "parent_id"}`` rows."""
    resp = httpx.get(
        f"{supabase_url}/rest/v1/categories",
        headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
        params={"user_id": f"eq.{user_id}", "select": "id,name,parent_id", "order": "name.asc"},
        timeout=10.0,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected response reading categories for a restore.")
    return {r["name"]: r for r in rows if r.get("name") and r.get("id")}


def _depth(name: str, by_name: Dict[str, Dict[str, Any]]) -> int:
    by_id = {r["id"]: r for r in by_name.values()}
    depth, row, seen = 1, by_name[name], set()
    while row.get("parent_id") and row["parent_id"] in by_id and row["id"] not in seen:
        seen.add(row["id"])
        row = by_id[row["parent_id"]]
        depth += 1
    return depth


def clean_path(path: Sequence[Any]) -> Tuple[str, ...]:
    """A category path as the restore keys it: each name stripped, empty names dropped."""
    return tuple(s for s in (str(n).strip() for n in path) if s)


def ensure_category_paths(
    user_id: str,
    paths: Iterable[Sequence[str]],
    *,
    supabase_url: str,
    service_key: str,
) -> Dict[Tuple[str, ...], str]:
    """Create the missing nodes of ``paths``; return each path's deepest placed name.

    The value is the path's leaf, or a shallower node when depth forbade the rest.
    """
    if live_writes_blocked():
        raise RuntimeError(
            "Refusing to create categories during a test run: it would write to a real "
            "Supabase project. See recipeparser.config.live_writes_blocked."
        )
    clean: List[Tuple[str, ...]] = []
    for p in paths:
        t = clean_path(p)
        if t and t not in clean:
            clean.append(t)
    if not clean:
        return {}

    by_name = _read(user_id, supabase_url, service_key)
    for level in range(max(len(p) for p in clean)):
        to_create: Dict[str, Optional[str]] = {}
        for path in clean:
            if len(path) <= level or path[level] in by_name or path[level] in to_create:
                continue
            if level == 0:
                to_create[path[0]] = None
                continue
            parent = by_name.get(path[level - 1])
            if parent is None:  # the chain above was cut short
                continue
            if _depth(parent["name"], by_name) + 1 > MAX_DEPTH:
                log.warning("Restore: %r is nested too deep to create under %r.", path[level], parent["name"])
                continue
            to_create[path[level]] = parent["id"]
        if not to_create:
            continue
        resp = httpx.post(
            f"{supabase_url}/rest/v1/categories",
            headers={
                "Authorization": f"Bearer {service_key}",
                "apikey": service_key,
                "Content-Type": "application/json",
                "Prefer": "resolution=ignore-duplicates,return=minimal",
            },
            params={"on_conflict": "user_id,name"},
            json=[
                {"id": str(uuid.uuid4()), "user_id": user_id, "name": n, "parent_id": pid}
                for n, pid in to_create.items()
            ],
            timeout=15.0,
        )
        resp.raise_for_status()
        by_name = _read(user_id, supabase_url, service_key)

    placed: Dict[Tuple[str, ...], str] = {}
    for path in clean:
        deepest = None
        for name in path:
            if name not in by_name:
                break
            deepest = name
        if deepest is not None:
            placed[path] = deepest
    return placed
