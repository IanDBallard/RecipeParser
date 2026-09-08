"""
recipeparser/core/regen.py — pure helpers for regenerating derived recipe data.

Spec 5.2: no I/O here.  The adapter (recipeparser/adapters/regen_worker.py)
reads rows, calls these, and writes results.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Tuple

from recipeparser.models import CayenneRefinement, RecipeExtraction, StructuredIngredient, TokenizedDirection

_FAT_TOKEN_RE = re.compile(r"\{\{[^|]+\|([^}]+)\}\}")


def strip_fat_tokens(text: str) -> str:
    """'Mix {{ing_01|flour}}' -> 'Mix flour'."""
    return _FAT_TOKEN_RE.sub(r"\1", text)


def raw_lines_from_derived(
    structured: List[StructuredIngredient],
    tokenized: List[TokenizedDirection],
) -> Tuple[List[str], List[str]]:
    """Raw ingredient lines and direction steps recovered from derived data (spec 5.7)."""
    lines = [ing.fallback_string for ing in structured]
    steps = [strip_fat_tokens(step.text) for step in tokenized]
    return lines, steps


def build_extraction(row: Mapping[str, Any]) -> RecipeExtraction:
    """A REFINE input from a claimed recipes row (spec 5.4)."""
    return RecipeExtraction(
        name=str(row.get("title") or ""),
        ingredients=[str(s) for s in (row.get("ingredient_lines") or [])],
        directions=[str(s) for s in (row.get("direction_steps") or [])],
    )


def build_update(
    refinement: CayenneRefinement,
    embedding: List[float],
    read_rev: int,
) -> Dict[str, Any]:
    """
    The conditional write-back payload (spec 5.5).  Title, base_servings and
    grid_categories are deliberately absent: they are user-owned after ingest.
    amount_overrides is emptied because the new structured entries already
    reflect the rewritten lines.
    """
    return {
        "structured_ingredients": [i.model_dump() for i in refinement.structured_ingredients],
        "tokenized_directions": [d.model_dump() for d in refinement.tokenized_directions],
        "embedding": list(embedding),
        "derived_rev": read_rev,
        "amount_overrides": {},
        "derived_error": None,
        "derived_attempts": 0,
        "claimed_at": None,
    }
