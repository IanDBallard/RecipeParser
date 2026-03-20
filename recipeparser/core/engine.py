"""
core/engine.py — Pure orchestration engine for Project Cayenne.

ARCHITECTURAL INVARIANT:
  This module is pure — it performs no file I/O, no writer calls, and no
  adapter-layer operations.  It only orchestrates calls between the Gemini
  provider (via the injected client) and the models layer.

  Allowed imports: recipeparser.models, recipeparser.gemini, recipeparser.utils
  Forbidden imports: recipeparser.export, recipeparser.supabase_writer,
                     recipeparser.io.*, recipeparser.adapters.*
"""
from __future__ import annotations

import logging
from typing import List, Set

from recipeparser.models import RecipeExtraction

log = logging.getLogger(__name__)


def deduplicate_recipes(recipes: List[RecipeExtraction]) -> List[RecipeExtraction]:
    """
    Remove duplicate recipes based on a normalised version of the name.
    Keeps the first occurrence (preserves chapter order).
    """
    seen: Set[str] = set()
    unique: List[RecipeExtraction] = []

    for recipe in recipes:
        key = recipe.name.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(recipe)
        else:
            log.info("Duplicate recipe skipped: '%s'", recipe.name)

    return unique


