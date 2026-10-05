"""
supabase.py — write_recipe_to_supabase: writes completed recipes to Supabase.

ARCHITECTURAL INVARIANT:
  The RecipeParser API is the sole writer of ingested recipe data to Supabase.
  The client app NEVER receives recipe JSON in an HTTP response and NEVER writes
  ingested recipes to Supabase itself. Recipes reach the client via PowerSync sync.

This module uses the SUPABASE_SERVICE_ROLE_KEY (service-role key) which bypasses RLS.
It must NEVER be called from the mobile client — only from the FastAPI backend.

Required env vars:
  SUPABASE_URL              — e.g. https://<ref>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY — service-role key (never the anon key)
"""

import json
import logging
import os
import uuid
from typing import Callable, Dict, List, Optional, Set, Tuple

import httpx
from dotenv import load_dotenv

from recipeparser.config import live_writes_blocked
from recipeparser.models import IngestResponse

load_dotenv()
log = logging.getLogger(__name__)


def _get_creds() -> Tuple[str, str]:
    """Return (supabase_url, service_key). Raises RuntimeError if not configured."""
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — "
            "cannot write recipe to Supabase."
        )
    return url, key


# One category link the junction write could not make: {"category_id", "reason"}.
RefusedLink = Dict[str, str]

# The junction insert's conflict target. Each row carries a fresh id, so without it
# PostgREST arbitrates on the primary key, which never fires, and a link that is
# already there is refused with a 409 instead of ignored. RecatWorker upserts on
# the same pair.
_ON_PAIR = {"on_conflict": "recipe_id,category_id"}


def _write_category_junctions(
    recipe_id: str,
    user_id: str,
    grid_categories: Dict[str, List[str]],
    category_ids: Dict[str, str],
    supabase_url: str,
    service_key: str,
) -> List[RefusedLink]:
    """
    Insert rows into the ``recipe_categories`` junction table for each tag
    in ``grid_categories`` that has a matching UUID in ``category_ids``.

    This is a best-effort write — failures are logged but do NOT raise, so
    the recipe row is never rolled back due to a junction table error. A
    refused batch is retried row by row, so one bad category id costs only
    its own link (Fix Roadmap F-004). So is a batch that got no answer at all
    (F-128): the insert ignores a duplicate of the (recipe_id, category_id) pair
    (``_ON_PAIR``), so the rows are safe to send again whether or not the batch
    landed.

    Returns the links it could not write, each with why, so the job that
    wrote the recipe can account for them (Fix Roadmap F-115, 2026-09-29):
    until then a lost link reached only this module's log.

    Args:
        recipe_id:       UUID of the newly-inserted recipe row.
        user_id:         Authenticated user's UUID (denormalized for PowerSync).
        grid_categories: Multipolar result dict, e.g. {"Cuisine": ["Italian"]}.
        category_ids:    Mapping of category_name → UUID from SupabaseCategorySource.
        supabase_url:    Base Supabase URL (already stripped of trailing slash).
        service_key:     Service-role key.
    """
    if not grid_categories or not category_ids:
        return []

    # Collect all tag names from the grid, deduplicate
    all_tags: List[str] = []
    seen_tags: Set[str] = set()
    for tags in grid_categories.values():
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if tag and tag not in seen_tags:
                seen_tags.add(tag)
                all_tags.append(tag)

    if not all_tags:
        return []

    # Build junction rows — only for tags that have a known UUID
    rows: List[Dict[str, str]] = []
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
        return []

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
            params=_ON_PAIR,
            json=rows,
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        # No answer, so none of these links is known to have landed, and until
        # F-128 every one was given up here. `Prefer: resolution=ignore-duplicates`
        # on the pair (`_ON_PAIR`) makes a second insert of a link that did land a
        # no-op, so the rows below are safe to send again.
        log.warning(
            "Junction write: network error for recipe %s (%s) — retrying each of "
            "%d row(s) on its own.",
            recipe_id, exc, len(rows),
        )
    else:
        if resp.status_code in (200, 201):
            log.info(
                "Junction write: %d recipe_categories rows inserted for recipe %s.",
                len(rows),
                recipe_id,
            )
            return []

        # One request is all-or-nothing, so a single bad row — a category the cook
        # deleted while the import ran, which the foreign key then refuses — used to
        # cost the recipe EVERY category link (Fix Roadmap F-004). Retry row by row
        # so only the refused links are lost. Refusal is the rare path and a recipe
        # carries a handful of tags, so the extra requests cost nothing in practice.
        log.warning(
            "Junction write: batch INSERT refused [%s] for recipe %s (%s) — retrying "
            "each of %d row(s) on its own.",
            resp.status_code,
            recipe_id,
            resp.text[:300],
            len(rows),
        )
    written = 0
    refused: List[RefusedLink] = []
    for row in rows:
        try:
            one = httpx.post(
                f"{supabase_url}/rest/v1/recipe_categories",
                headers=headers,
                params=_ON_PAIR,
                json=[row],
                timeout=15.0,
            )
        except httpx.RequestError as exc:
            log.warning(
                "Junction write: network error linking recipe %s to category %s: %s",
                recipe_id, row["category_id"], exc,
            )
            refused.append({"category_id": row["category_id"], "reason": f"network error: {exc}"})
            continue
        if one.status_code in (200, 201):
            written += 1
        else:
            log.warning(
                "Junction write: category %s refused for recipe %s [%s]: %s",
                row["category_id"], recipe_id, one.status_code, one.text[:300],
            )
            refused.append({
                "category_id": row["category_id"],
                "reason": f"[{one.status_code}] {one.text[:300]}",
            })
    log.info(
        "Junction write: %d of %d recipe_categories rows inserted for recipe %s "
        "after the batch failed.",
        written, len(rows), recipe_id,
    )
    return refused


def write_recipe_categories(
    recipe_id: str,
    user_id: str,
    grid_categories: Dict[str, List[str]],
    category_ids: Dict[str, str],
    on_link_refused: Optional[Callable[[RefusedLink], None]] = None,
) -> None:
    """
    Write ``recipe_categories`` links for a recipe already written: the import's
    TAG stage tags every ten recipes after they are in the table (Cayenne Fix
    Roadmap F-246). Best-effort exactly as the links written with a recipe are:
    a refused row costs only its own link and is reported through
    ``on_link_refused``; a duplicate pair is ignored.
    """
    if not grid_categories or not category_ids:
        return
    if live_writes_blocked():
        raise RuntimeError(
            "Live writes blocked: this process is under pytest and "
            "ALLOW_LIVE_WRITES_IN_TESTS is not set to '1' — refusing to write "
            "to a real Supabase project. See recipeparser.config.live_writes_blocked."
        )
    supabase_url, service_key = _get_creds()
    refused = _write_category_junctions(
        recipe_id=recipe_id,
        user_id=user_id,
        grid_categories=grid_categories,
        category_ids=category_ids,
        supabase_url=supabase_url,
        service_key=service_key,
    )
    if on_link_refused is not None:
        for link in refused:
            on_link_refused(link)


def write_recipe_to_supabase(
    recipe: IngestResponse,
    user_id: str,
    recipe_id: Optional[str] = None,
    category_ids: Optional[Dict[str, str]] = None,
    on_link_refused: Optional[Callable[[RefusedLink], None]] = None,
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
        on_link_refused: Optional callback, called once for each category link
                       the junction write could not make (Fix Roadmap F-115).
                       The recipe row is written either way.

    Returns:
        The UUID string of the inserted recipe row.

    Raises:
        RuntimeError: If env vars are missing, this is a test run that must not
            reach a real project (see ``recipeparser.config.live_writes_blocked``),
            or the Supabase insert fails.
    """
    if live_writes_blocked():
        raise RuntimeError(
            "Live writes blocked: this process is under pytest and "
            "ALLOW_LIVE_WRITES_IN_TESTS is not set to '1' — refusing to write "
            "to a real Supabase project. See recipeparser.config.live_writes_blocked."
        )
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
        # Plain text and smallint - not jsonb, so the array rule below does not
        # apply. rating is null when unrated; the column's check rejects 0.
        "source": recipe.source,
        "notes": recipe.notes,
        "rating": recipe.rating,
        "nutritional_info": recipe.nutritional_info,
        "description": recipe.description,
        "difficulty": recipe.difficulty,
        # Citation columns (migration recipe_source_citation in the Cayenne repo).
        "source_kind": recipe.source_kind,
        "source_key": recipe.source_key,
        "source_title": recipe.source_title,
        "source_author": recipe.source_author,
        # What REFINE detected (verbatim ingestion D5, Cayenne migration verbatim_ingestion).
        "source_uom_system_detected": recipe.source_uom_system_detected,
        "source_uom_system_evidence": recipe.source_uom_system_evidence,
        # jsonb columns — send the list itself. json.dumps()ing it here handed
        # Postgres a JSON *string* containing an array, and that is what jsonb
        # stored: on 2026-09-06 jsonb_typeof reported 'string' for all 786 rows
        # in the live library, with no arrays in the table at all. It went
        # unnoticed because the Cayenne client compensates (kitchenRecipe.ts's
        # parseArrayColumn parses twice). PostgREST takes a real list for jsonb,
        # exactly as it already does for the pgvector embedding below.
        "structured_ingredients": [ing.model_dump() for ing in recipe.structured_ingredients],
        "tokenized_directions": [d.model_dump() for d in recipe.tokenized_directions],
        # vector(1536) — PostgREST accepts a JSON array for pgvector columns
        "embedding": recipe.embedding,
        # Raw, user-owned body + derived bookkeeping (spec 3.2/3.3). A freshly
        # ingested recipe is never stale: body_rev == derived_rev == 0.
        "ingredient_lines": list(recipe.ingredient_lines),
        "direction_steps": list(recipe.direction_steps),
        "body_rev": 0,
        "derived_rev": 0,
        "amount_overrides": {},
        # Structured durations and servings (spec 3.6).
        "prep_min_minutes": recipe.prep_min_minutes,
        "prep_max_minutes": recipe.prep_max_minutes,
        "prep_note": recipe.prep_note,
        "cook_min_minutes": recipe.cook_min_minutes,
        "cook_max_minutes": recipe.cook_max_minutes,
        "cook_note": recipe.cook_note,
        "servings_min": recipe.servings_min,
        "servings_max": recipe.servings_max,
        "servings_note": recipe.servings_note,
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

    # Write recipe_categories junction rows (best-effort — non-fatal; a refused
    # row costs only its own link, see _write_category_junctions)
    grid = getattr(recipe, "grid_categories", None) or {}
    if grid and category_ids:
        refused = _write_category_junctions(
            recipe_id=rid,
            user_id=user_id,
            grid_categories=grid,
            category_ids=category_ids,
            supabase_url=supabase_url,
            service_key=service_key,
        )
        if on_link_refused is not None:
            for link in refused:
                on_link_refused(link)

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
) -> List[str]:
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

    errors: List[str] = []

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
