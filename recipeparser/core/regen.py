"""
recipeparser/core/regen.py — pure helpers for regenerating derived recipe data.

Spec 5.2: no I/O here.  The adapter (recipeparser/adapters/regen_worker.py)
reads rows, calls these, and writes results.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from recipeparser.core.fat_tokens import TOKEN_RE, is_pan_id, strip_fat_tokens
from recipeparser.core.pans import check_pan_tokens
from recipeparser.models import CayenneRefinement, RecipeExtraction, StructuredIngredient, TokenizedDirection

__all__ = ["strip_fat_tokens", "raw_lines_from_derived", "raw_body_column", "build_extraction", "build_update"]


def raw_lines_from_derived(
    structured: List[StructuredIngredient],
    tokenized: List[TokenizedDirection],
) -> Tuple[List[str], List[str]]:
    """Raw ingredient lines and direction steps recovered from derived data (spec 5.7)."""
    lines = [ing.fallback_string for ing in structured]
    steps = [strip_fat_tokens(step.text) for step in tokenized]
    return lines, steps


def raw_body_column(row: Mapping[str, Any], column: str) -> List[str]:
    """
    One raw body column (``ingredient_lines`` / ``direction_steps``) as strings.

    Missing or null is an empty list; anything else that is not a list is a hard
    error.  A double-encoded jsonb column arrives as a ``str``, and iterating a
    ``str`` yields characters: REFINE would receive forty single-character
    "ingredients", succeed, and the worker would write that garbage back as
    derived data under the body_rev guard — silent corruption with no error and
    no attempt counter.  This is not hypothetical; ``io/writers/supabase.py``
    records that all 786 rows in the live library once carried exactly this
    double-encoding on ``structured_ingredients``, unnoticed because the client
    compensated for it.  Raising sends the row to ``derived_error`` via the
    ``regen_failed`` RPC, where the attempt cap stops it after three tries.
    """
    value = row.get(column)
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(
            "build_extraction(): {} must be a list, got {} ({:.60}) — the column "
            "is probably double-encoded jsonb.".format(column, type(value).__name__, repr(value))
        )
    return [str(s) for s in value]


def build_extraction(row: Mapping[str, Any]) -> RecipeExtraction:
    """
    A REFINE input from a claimed recipes row (spec 5.4).

    A null (or absent) body column is refused here rather than read as ``[]``
    (Fix Roadmap F-005). Read as empty, REFINE got nothing, succeeded, and the
    worker wrote empty derived ingredients back as fresh — the recipe lost its
    ingredients with no error. An empty list is a real body (the cook deleted
    every line); a null is not. Both columns are ``NOT NULL DEFAULT '[]'`` and
    every pre-existing row was backfilled from its derived data when they were
    added (Cayenne migration 013), so no row written through the schema can be
    null — one that is came from somewhere this code does not understand.
    Falling back to ``raw_lines_from_derived`` is not open to us either: the
    claim RPC returns only the raw columns. So it fails, the worker records it
    through ``regen_failed``, and the attempt cap stops it after three tries
    with the reason in ``derived_error``.

    ``raw_body_column`` keeps its null-as-empty reading because the recategorise
    prompt shares it, and there an empty body only makes a weaker prompt.

    Raises:
        ValueError: if ``ingredient_lines`` or ``direction_steps`` is null or absent.
        TypeError: if ``ingredient_lines`` or ``direction_steps`` is present but
            is not a list — see ``raw_body_column``.
    """
    for column in ("ingredient_lines", "direction_steps"):
        if row.get(column) is None:
            raise ValueError(
                "build_extraction(): {} is null — refusing to regenerate the recipe "
                "from an empty body.".format(column)
            )
    return RecipeExtraction(
        name=str(row.get("title") or ""),
        ingredients=raw_body_column(row, "ingredient_lines"),
        directions=raw_body_column(row, "direction_steps"),
    )


def _fold(text: str) -> str:
    return " ".join(text.lower().split())


def build_update(
    refinement: CayenneRefinement,
    embedding: List[float],
    read_rev: int,
    current_pans: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """
    The conditional write-back payload (spec 5.5).  Title, base_servings and
    grid_categories are deliberately absent: they are user-owned after ingest.
    amount_overrides is emptied because the new structured entries already
    reflect the rewritten lines. The detected system is derived data, so an edit refreshes it (D5).

    Tins, bake type and step are derived data like the detected system, with one exception
    (pan-aware scaling, parser contract): a tin the cook edited is theirs, so when any current tin
    is edited the cook's tins are written back unchanged and REFINE's pan tokens for other ids unwrap.
    Accepted race, not guarded: a pans edit the cook has made that has not synced when regen reads
    can be overwritten.
    """
    edited = bool(current_pans) and any(isinstance(p, dict) and p.get("edited") is True for p in current_pans or [])
    directions = refinement.tokenized_directions
    pans: List[Any] = [p.model_dump() for p in refinement.pans]
    if edited:
        pans = list(current_pans or [])
        # A tombstone (Cayenne F-291: a tin the cook removed, removed: true) is written back but is not a kept tin.
        live = [
            p for p in pans if isinstance(p, dict) and isinstance(p.get("id"), str) and p.get("removed") is not True
        ]
        keep = {p["id"] for p in live}
        # The stripped text is the source: a kept token survives when its words are in the recipe's own text.
        directions, _ = check_pan_tokens(directions, keep, strip_fat_tokens("\n".join(d.text for d in directions)))
        # R-F: REFINE may renumber tins, so a kept id must also still name this tin: its words sit in the cook's raw.
        raws = {
            p["id"]: _fold(str(p.get("raw") or ""))
            for p in live
        }

        def same_tin(m: "re.Match[str]") -> str:
            if is_pan_id(m.group(1)) and _fold(m.group(2)) not in raws.get(m.group(1), ""):
                return m.group(2)
            return m.group(0)

        directions = [d.model_copy(update={"text": TOKEN_RE.sub(same_tin, d.text)}) for d in directions]
    return {
        "structured_ingredients": [i.model_dump() for i in refinement.structured_ingredients],
        "tokenized_directions": [d.model_dump() for d in directions],
        "embedding": list(embedding),
        "derived_rev": read_rev,
        "amount_overrides": {},
        "derived_error": None,
        "derived_attempts": 0,
        "claimed_at": None,
        "source_uom_system_detected": refinement.source_uom_system_detected,
        "source_uom_system_evidence": refinement.source_uom_system_evidence,
        "pans": pans,
        "bake_type": refinement.bake_type,
        "bake_step": refinement.bake_step,
    }
