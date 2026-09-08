"""
recipeparser/core/regen.py — pure helpers for regenerating derived recipe data.

Spec 5.2: no I/O here.  The adapter (recipeparser/adapters/regen_worker.py)
reads rows, calls these, and writes results.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
from __future__ import annotations

import re
from typing import List, Tuple

from recipeparser.models import StructuredIngredient, TokenizedDirection

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
