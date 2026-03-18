"""
Supabase-backed category source for the API adapter.

Fetches the authenticated user's multipolar taxonomy from the Supabase
``categories`` table and returns both:

  1. ``load_axes()``  — axis_name → [tag, ...] dict for LLM prompt injection
  2. ``load_category_ids()`` — category_name → UUID dict for junction table writes

The ``categories`` table schema (from .clinerules):

    create table categories (
        id          uuid primary key default uuid_generate_v4(),
        user_id     uuid references auth.users not null,
        name        text not null,
        parent_id   uuid references categories(id),  -- nullable, supports nesting
        created_at  timestamp with time zone default timezone('utc'::text, now())
    );

Axis mapping strategy (mirrors the 4-layer faceted model):
  - Rows with ``parent_id IS NULL`` → axis names (Level 1)
  - Their direct children → tags under that axis (Level 2)
  - Deeper nesting (Level 3-4) is flattened into the nearest Level-2 axis

This source uses the SUPABASE_SERVICE_KEY to bypass RLS so it can read any
user's categories.  It must NEVER be called from the mobile client.

Required env vars (read from environment at call time — not at import):
  SUPABASE_URL         — e.g. https://<ref>.supabase.co
  SUPABASE_SERVICE_KEY — service-role key (never the anon key)
"""
import logging
import os
from typing import Dict, List, Optional

import httpx

from recipeparser.io.category_sources.base import CategorySource

log = logging.getLogger(__name__)


class SupabaseCategorySource(CategorySource):
    """
    Loads taxonomy axes and category UUIDs from Supabase.

    Args:
        supabase_url: Base URL of the Supabase project (e.g.
                      ``https://<ref>.supabase.co``).  Falls back to the
                      ``SUPABASE_URL`` env var if not provided.
        service_key:  Service-role key.  Falls back to the
                      ``SUPABASE_SERVICE_KEY`` env var if not provided.
    """

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        service_key: Optional[str] = None,
    ) -> None:
        self._url = (supabase_url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self._key = service_key or os.getenv("SUPABASE_SERVICE_KEY", "")

    # ------------------------------------------------------------------
    # CategorySource interface
    # ------------------------------------------------------------------

    def load_axes(self, user_id: str) -> Dict[str, List[str]]:
        """
        Fetch the user's category tree from Supabase and return it as a
        multipolar axis dict.

        Returns {} if:
        - Supabase credentials are not configured
        - The user has no categories defined
        - The network request fails (non-fatal — Zero-Tag Mandate)
        """
        rows = self._fetch_categories(user_id)
        if not rows:
            return {}

        axes = self._build_axes(rows)
        log.info(
            "SupabaseCategorySource: loaded %d axes for user %s.",
            len(axes),
            user_id,
        )
        return axes

    def load_category_ids(self, user_id: str) -> Dict[str, str]:
        """
        Return a flat mapping of category_name → UUID for all of the user's
        categories.  Used by the Supabase writer to populate the
        ``recipe_categories`` junction table.

        Returns {} if credentials are missing or the request fails.
        """
        rows = self._fetch_categories(user_id)
        if not rows:
            return {}

        # Build name → id map (last-write-wins for duplicate names)
        name_to_id: Dict[str, str] = {}
        for row in rows:
            name = row.get("name", "").strip()
            cat_id = row.get("id", "")
            if name and cat_id:
                name_to_id[name] = cat_id

        log.debug(
            "SupabaseCategorySource: %d category name→id mappings for user %s.",
            len(name_to_id),
            user_id,
        )
        return name_to_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_categories(self, user_id: str) -> List[dict]:
        """
        Query Supabase for all category rows belonging to ``user_id``.

        Returns a list of row dicts, or [] on any error.
        """
        if not self._url or not self._key:
            log.warning(
                "SupabaseCategorySource: SUPABASE_URL or SUPABASE_SERVICE_KEY "
                "not configured — returning empty axes."
            )
            return []

        endpoint = f"{self._url}/rest/v1/categories"
        headers = {
            "Authorization": f"Bearer {self._key}",
            "apikey": self._key,
        }
        params = {
            "user_id": f"eq.{user_id}",
            "select": "id,name,parent_id",
            "order": "name.asc",
        }

        try:
            resp = httpx.get(endpoint, headers=headers, params=params, timeout=10.0)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "SupabaseCategorySource: HTTP %s fetching categories for user %s: %s",
                exc.response.status_code,
                user_id,
                exc.response.text[:200],
            )
            return []
        except Exception as exc:
            log.warning(
                "SupabaseCategorySource: network error fetching categories for user %s: %s",
                user_id,
                exc,
            )
            return []

        rows = resp.json()
        if not isinstance(rows, list):
            log.warning(
                "SupabaseCategorySource: unexpected response shape for user %s.",
                user_id,
            )
            return []

        log.debug(
            "SupabaseCategorySource: fetched %d category rows for user %s.",
            len(rows),
            user_id,
        )
        return rows

    def _build_axes(self, rows: List[dict]) -> Dict[str, List[str]]:
        """
        Convert a flat list of category rows into a multipolar axis dict.

        Mapping:
        - parent_id IS NULL → axis (Level 1)
        - direct children of an axis → tags (Level 2)
        - deeper descendants are flattened into their nearest Level-2 ancestor

        Axes with no children are treated as a single-tag axis
        (axis name == tag name) to preserve the Zero-Tag Mandate semantics
        while still allowing the LLM to match against them.
        """
        # Build lookup structures
        id_to_name: Dict[str, str] = {}
        id_to_parent: Dict[str, Optional[str]] = {}
        children_by_parent: Dict[str, List[str]] = {}  # parent_id → [child_id, ...]

        for row in rows:
            cat_id = row.get("id", "")
            name = (row.get("name") or "").strip()
            parent_id = row.get("parent_id")  # None for top-level

            if not cat_id or not name:
                continue

            id_to_name[cat_id] = name
            id_to_parent[cat_id] = parent_id

            if parent_id is not None:
                children_by_parent.setdefault(parent_id, []).append(cat_id)

        # Identify top-level (axis) nodes
        top_level_ids = [cid for cid, parent in id_to_parent.items() if parent is None]

        axes: Dict[str, List[str]] = {}
        for axis_id in top_level_ids:
            axis_name = id_to_name.get(axis_id, "")
            if not axis_name:
                continue

            # Collect all descendants (BFS), flatten to tag names
            tag_names: List[str] = []
            queue = list(children_by_parent.get(axis_id, []))
            visited: set = set()
            while queue:
                child_id = queue.pop(0)
                if child_id in visited:
                    continue
                visited.add(child_id)
                child_name = id_to_name.get(child_id, "")
                if child_name:
                    tag_names.append(child_name)
                # Recurse into grandchildren (flattened)
                queue.extend(children_by_parent.get(child_id, []))

            if tag_names:
                axes[axis_name] = sorted(tag_names)
            else:
                # Leaf axis — single-tag axis (axis name == tag)
                axes[axis_name] = [axis_name]

        return axes
