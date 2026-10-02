"""
recipeparser/core/fat_tokens.py — the one Fat Token grammar, and the checks on what REFINE says.

A token is ``{{ingredient_id|words}}`` or ``{{ingredient_id|words|use}}`` (Cayenne's direction
amounts design, docs/superpowers/specs/2026-10-02-direction-amounts-design.md). ``words`` is the
direction's own text, so replacing every token with its words gives the step back as written.
``use`` says how much of the ingredient that mention uses: ``all``, ``rest``, ``none``, or an
amount (``0.5 cup``, ``60 g``, ``2``), in which case the token wraps only the quantity the source
wrote. A token without ``use`` is a legacy token, written before 9.5.0.

Pure: no I/O, no imports from recipeparser.io or recipeparser.adapters.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from recipeparser.models import DirectionMention, StructuredIngredient, TokenizedDirection

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"\{\{([^|}]+)\|([^|}]+)(?:\|([^|}]*))?\}\}")
_PART_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:\s+(\S.*))?$")
_KEYWORDS = ("all", "rest", "none")
# Parts may exceed the line by this much before they are called wrong: "about" is everywhere.
_PART_SLACK = 1.05

_UNIT_ALIASES: Dict[str, str] = {
    "cups": "cup", "c": "cup",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsps": "tbsp", "tbs": "tbsp", "tbl": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsps": "tsp",
    "gram": "g", "grams": "g", "gr": "g", "gm": "g",
    "kilogram": "kg", "kilograms": "kg", "kgs": "kg",
    "ounce": "oz", "ounces": "oz",
    "pound": "lb", "pounds": "lb", "lbs": "lb",
    "millilitre": "ml", "millilitres": "ml", "milliliter": "ml", "milliliters": "ml",
    "litre": "l", "litres": "l", "liter": "l", "liters": "l",
}


class Use(NamedTuple):
    kind: str  # 'legacy' | 'all' | 'rest' | 'none' | 'part' | 'invalid'
    amount: Optional[float] = None
    unit: Optional[str] = None


def strip_fat_tokens(text: str) -> str:
    """'Mix {{ing_01|flour|all}}' -> 'Mix flour': every token replaced by its words."""
    return TOKEN_RE.sub(r"\2", text)


def parse_use(raw: Optional[str]) -> Use:
    """A token's third field, read the way the Cayenne client reads it (``directionMentions.ts``)."""
    if raw is None:
        return Use("legacy")
    use = raw.strip().lower()
    if use in _KEYWORDS:
        return Use(use)
    match = _PART_RE.match(raw.strip())
    if not match:
        return Use("invalid")
    amount = float(match.group(1))
    if amount <= 0:
        return Use("invalid")
    return Use("part", amount, match.group(2))


def _unit_key(unit: Optional[str]) -> Optional[str]:
    if unit is None:
        return None
    key = unit.strip().lower().rstrip(".")
    return _UNIT_ALIASES.get(key, key)


def _token(ingredient_id: str, words: str, use: str) -> str:
    return "{{" + ingredient_id + "|" + words + "|" + use + "}}"


def check_mentions(
    ingredients: Sequence[StructuredIngredient],
    directions: Sequence[TokenizedDirection],
) -> Tuple[List[TokenizedDirection], List[str]]:
    """
    Demote the uses the arithmetic cannot support to ``none``, so no mention claims a number it
    cannot have. Returns the directions, rewritten where needed, and one line per demotion.

    - an amount that does not parse;
    - every part (and remainder) of an ingredient whose parts, in its own unit, add up to more
      than the line gives — one of them is wrong, and nothing says which;
    - ``all`` beside a part or a remainder of the same ingredient.

    A legacy token is left alone: the client has its own rule for those. The client applies these
    same checks again (direction amounts design, D10); this is the import's half, and it logs.
    """
    by_id = {ing.id: ing for ing in ingredients}
    uses: Dict[str, List[Use]] = {}
    for step in directions:
        for match in TOKEN_RE.finditer(step.text):
            uses.setdefault(match.group(1), []).append(parse_use(match.group(3)))

    over: Set[str] = set()
    for ing_id, found in uses.items():
        ing = by_id.get(ing_id)
        parts = [u for u in found if u.kind == "part"]
        if ing is None or ing.amount is None or not parts:
            continue
        if all(_unit_key(p.unit) == _unit_key(ing.unit) for p in parts):
            if sum(p.amount or 0 for p in parts) > ing.amount * _PART_SLACK:
                over.add(ing_id)

    demotions: List[str] = []

    def rewrite(match: "re.Match[str]") -> str:
        ing_id, words, raw = match.group(1), match.group(2), match.group(3)
        use = parse_use(raw)
        found = uses.get(ing_id, [])
        reason = None
        if use.kind == "invalid":
            reason = f"amount {raw!r} does not parse"
        elif use.kind in ("part", "rest") and ing_id in over:
            reason = "its parts add up to more than the ingredient line"
        elif use.kind == "all" and any(u.kind in ("part", "rest") for u in found):
            reason = "'all' beside a part of the same ingredient"
        if reason is None:
            return match.group(0)
        demotions.append(f"{ing_id} {words!r}: {reason}")
        return _token(ing_id, words, "none")

    checked = [TokenizedDirection(step=d.step, text=TOKEN_RE.sub(rewrite, d.text)) for d in directions]
    return checked, demotions


def _locate(text: str, cursor: int, quote: str, context: str) -> int:
    """
    Where ``quote`` starts in ``text``, at or after ``cursor``, or -1. The context places it: the
    first occurrence of the context whose quote lies past the cursor. Without a usable context only
    an unambiguous quote is placed — "add 1/2 cup flour; add more flour" has two, and the wrong one
    would put "none" on the flour the part already measured.
    """
    if context and quote in context:
        offset = context.index(quote)
        start = text.find(context)
        while start >= 0:
            if start + offset >= cursor:
                return start + offset
            start = text.find(context, start + 1)
    at = text.find(quote, cursor)
    if at >= 0 and text.find(quote, at + 1) < 0:
        return at
    return -1


def splice_mentions(
    steps: Sequence[str],
    mentions: Sequence[DirectionMention],
    valid_ids: Sequence[str],
) -> Tuple[List[TokenizedDirection], List[str]]:
    """
    Tokenized directions built from the raw steps and the mentions a model found in them. The text
    is the raw step's, always: a token is placed only where its quote is found verbatim, after the
    previous token in the same step, so a model that misquotes loses that chip, never a word of the
    recipe. Returns the directions and one line per mention that could not be placed.
    """
    valid = set(valid_ids)
    by_step: Dict[int, List[DirectionMention]] = {}
    dropped: List[str] = []
    for m in mentions:
        if m.ingredient_id not in valid:
            dropped.append(f"step {m.step}: unknown ingredient {m.ingredient_id!r}")
        elif not m.quote or any(c in m.quote for c in "{}|"):
            dropped.append(f"step {m.step}: unusable quote {m.quote!r}")
        elif not 1 <= m.step <= len(steps):
            dropped.append(f"step {m.step}: no such step")
        else:
            by_step.setdefault(m.step, []).append(m)

    out: List[TokenizedDirection] = []
    for index, text in enumerate(steps):
        pieces: List[str] = []
        cursor = 0
        for m in by_step.get(index + 1, []):
            at = _locate(text, cursor, m.quote, m.context)
            if at < 0:
                dropped.append(f"step {m.step}: {m.quote!r} ({m.context!r}) not placed after position {cursor}")
                continue
            use = m.use.strip()
            pieces.append(text[cursor:at])
            pieces.append(_token(m.ingredient_id, m.quote, use.lower() if use.lower() in _KEYWORDS else use))
            cursor = at + len(m.quote)
        pieces.append(text[cursor:])
        out.append(TokenizedDirection(step=index + 1, text="".join(pieces)))
    return out, dropped


def is_tagged(directions: Sequence[TokenizedDirection]) -> bool:
    """Whether the directions carry tokens and every one says its use: written by 9.5.0 or later."""
    found = [match.group(3) for d in directions for match in TOKEN_RE.finditer(d.text)]
    return bool(found) and all(use is not None for use in found)
