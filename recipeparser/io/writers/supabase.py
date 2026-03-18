"""
supabase_writer.py — Writes completed recipes directly to Supabase.

ARCHITECTURAL INVARIANT:
  The RecipeParser API is the sole writer of ingested recipe data to Supabase.
  The client app NEVER receives recipe JSON in an HTTP response and NEVER writes
  ingested recipes to Supabase itself. Recipes reach the client via PowerSync sync.

This module uses the SUPABASE_SERVICE_KEY (service-role key) which bypasses RLS.
It must NEVER be called from the mobile client — only from the FastAPI backend.

Required env vars:
  SUPABASE_URL         — e.g. https://<ref>.supabase.co
  SUPABASE_SERVICE_KEY — service-role key (never the anon key)
"""

import json
import logging
import os
import uuid
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv

from recipeparser.models import IngestResponse

load_dotenv()
log = logging.getLogger(__name__)


def _get_creds() -> tuple[str, str]:
    """Return (supabase_url, service_key). Raises RuntimeError if not configured."""
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL or SUPABASE_SERVICE_KEY not set — "
            "cannot write recipe to Supabase."
        )
    return url, key


def _write_category_junctions(
    recipe_id: str,
    user_id: str,
    grid_categories: Dict[str, List[str]],
    category_ids: Dict[str, str],
    supabase_url: str,
    service_key: str,
) -> None:
    """
    Insert rows into the ``recipe_categories`` junction table for each tag
    in ``grid_categories`` that has a matching UUID in ``category_ids``.

    This is a best-effort write — failures are logged but do NOT raise, so
    the recipe row is never rolled back due to a junction table error.

    Args:
        recipe_id:       UUID of the newly-inserted recipe row.
        user_id:         Authenticated user's UUID (denormalized for PowerSync).
        grid_categories: Multipolar result dict, e.g. {"Cuisine": ["Italian"]}.
        category_ids:    Mapping of category_name → UUID from SupabaseCategorySource.
        supabase_url:    Base Supabase URL (already stripped of trailing slash).
        service_key:     Service-role key.
    """
    if not grid_categories or not category_ids:
        return

    # Collect all tag names from the grid, deduplicate
    all_tags: List[str] = []
    seen_tags: set = set()
    for tags in grid_categories.values():
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if tag and tag not in seen_tags:
                seen_tags.add(tag)
                all_tags.append(tag)

    if not all_tags:
        return

    # Build junction rows — only for tags that have a known UUID
    rows: List[dict] = []
    for tag in all_tags:
        cat_id = category_ids.get(tag)
        if not cat_id:
            log.debug(
                "Junction write: no UUID found for tag %r — skipping.", tag
            )
            continue
        rows.append(
            {
                "id": str(uuid.uuid4()),          # required by PowerSync
                "recipe_id": recipe_id,
                "category_id": cat_id,
                "user_id": user_id,               # denormalized for PowerSync bucket
            }
        )

    if not rows:
        log.debug(
            "Junction write: no matching category UUIDs for recipe %s — skipping.",
            recipe_id,
        )
        return

    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/json",
        "Prefer": "return=minimal,resolution=ignore-duplicates",
    }

    try:
        resp = httpx.post(
            f"{supabase_url}/rest/v1/recipe_categories",
            headers=headers,
            json=rows,
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        log.warning(
            "Junction write: network error for recipe %s: %s", recipe_id, exc
        )
        return

    if resp.status_code not in (200, 201):
        log.warning(
            "Junction write: Supabase INSERT failed [%s] for recipe %s: %s",
            resp.status_code,
            recipe_id,
            resp.text[:300],
        )
        return

    log.info(
        "Junction write: %d recipe_categories rows inserted for recipe %s.",
        len(rows),
        recipe_id,
    )


def write_recipe_to_supabase(
    recipe: IngestResponse,
    user_id: str,
    recipe_id: Optional[str] = None,
    category_ids: Optional[Dict[str, str]] = None,
) -> str:
    """
    Persist a completed IngestResponse to the Supabase `recipes` table,
    then write ``recipe_categories`` junction rows for any grid_categories
    tags that have matching UUIDs in ``category_ids``.

    Uses the service-role key to bypass RLS (server-side write).
    The row will be synced to the client device via PowerSync.

    Args:
        recipe:        The fully-processed IngestResponse from the pipeline.
        user_id:       The authenticated user's UUID (from JWT `sub` claim).
        recipe_id:     Optional pre-generated UUID. A new one is generated if omitted.
        category_ids:  Optional mapping of category_name → UUID from
                       SupabaseCategorySource.load_category_ids().  When provided
                       and the recipe has grid_categories, junction rows are written
                       to ``recipe_categories``.  When None or empty, no junction
                       rows are written (Zero-Tag Mandate).

    Returns:
        The UUID string of the inserted recipe row.

    Raises:
        RuntimeError: If env vars are missing or the Supabase insert fails.
    """
    supabase_url, service_key = _get_creds()
    rid = recipe_id or str(uuid.uuid4())

    row = {
        "id": rid,
        "user_id": user_id,
        "title": recipe.title,
        "prep_time": recipe.prep_time,
        "cook_time": recipe.cook_time,
        "base_servings": recipe.base_servings,
        "source_url": recipe.source_url,
        "image_url": recipe.image_url,
        # jsonb columns — send as JSON strings via PostgREST
        "structured_ingredients": json.dumps(
            [ing.model_dump() for ing in recipe.structured_ingredients]
        ),
        "tokenized_directions": json.dumps(
            [d.model_dump() for d in recipe.tokenized_directions]
        ),
        # vector(1536) — PostgREST accepts a JSON array for pgvector columns
        "embedding": recipe.embedding,
    }

    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",  # don't echo the row back — saves bandwidth
    }

    try:
        resp = httpx.post(
            f"{supabase_url}/rest/v1/recipes",
            headers=headers,
            json=row,
            timeout=20.0,
        )
    except httpx.RequestError as exc:
        raise RuntimeError(f"Network error writing recipe to Supabase: {exc}") from exc

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Supabase INSERT failed [{resp.status_code}]: {resp.text[:400]}"
        )

    log.info("Recipe written to Supabase: id=%s title=%r user=%s", rid, recipe.title, user_id)

    # Write recipe_categories junction rows (best-effort — non-fatal)
    grid = getattr(recipe, "grid_categories", None) or {}
    if grid and category_ids:
        _write_category_junctions(
            recipe_id=rid,
            user_id=user_id,
            grid_categories=grid,
            category_ids=category_ids,
            supabase_url=supabase_url,
            service_key=service_key,
        )

    return rid


def delete_recipe_from_supabase(recipe_id: str) -> None:
    """
    Delete a recipe row by ID. Best-effort — logs on failure but does not raise.

    Used by the live test harness for cleanup after each test run.
    """
    try:
        supabase_url, service_key = _get_creds()
        resp = httpx.delete(
            f"{supabase_url}/rest/v1/recipes",
            params={"id": f"eq.{recipe_id}"},
            headers={
                "Authorization": f"Bearer {service_key}",
                "apikey": service_key,
            },
            timeout=15.0,
        )
        if resp.status_code in (200, 204):
            log.info("Recipe deleted from Supabase: id=%s", recipe_id)
        else:
            log.warning(
                "Recipe delete returned %s: %s", resp.status_code, resp.text[:200]
            )
    except Exception as exc:
        log.warning("Recipe delete failed: %s", exc)


def verify_recipe_in_supabase(
    recipe_id: str,
    expected_title: str,
    expected_ing_count: int,
) -> list[str]:
    """
    Read a recipe back from Supabase and validate key fields.

    Returns a list of error strings. An empty list means all checks passed.
    Used by the live test harness to confirm the API wrote correctly.
    """
    supabase_url, service_key = _get_creds()

    try:
        resp = httpx.get(
            f"{supabase_url}/rest/v1/recipes",
            params={
                "id": f"eq.{recipe_id}",
                "select": "id,title,structured_ingredients,tokenized_directions,embedding",
            },
            headers={
                "Authorization": f"Bearer {service_key}",
                "apikey": service_key,
            },
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        return [f"Network error reading recipe from Supabase: {exc}"]

    errors: list[str] = []

    if resp.status_code != 200:
        return [f"DB read failed [{resp.status_code}]: {resp.text[:200]}"]

    rows = resp.json()
    if not rows:
        return [f"Recipe {recipe_id!r} not found in Supabase after API write"]

    row = rows[0]

    if row.get("title") != expected_title:
        errors.append(
            f"title mismatch: got {row.get('title')!r}, expected {expected_title!r}"
        )

    ings = row.get("structured_ingredients", [])
    if isinstance(ings, str):
        ings = json.loads(ings)
    if len(ings) != expected_ing_count:
        errors.append(
            f"ingredient count: got {len(ings)}, expected {expected_ing_count}"
        )

    emb = row.get("embedding")
    if emb is None:
        errors.append("embedding is NULL in Supabase")
    elif isinstance(emb, list) and len(emb) != 1536:
        errors.append(f"embedding length {len(emb)}, expected 1536")

    return errors
