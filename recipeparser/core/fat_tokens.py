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


# A token for an amount must wrap a quantity: a digit, a fraction, or a number word. The model has
# been seen putting the amount on the ingredient's name instead ("Place the {{ing_01|butter|175 g}}"),
# which a quantity-only chip would print as "Place the 175 g".
QUANTITY_WORDS = re.compile(
    r"\d|[¼½¾⅐-⅞]|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"half|quarter|third|thirds|dozen|couple)\b",
    re.IGNORECASE,
)
# A quantity already in the text just before a token: "1 tablespoon of the ", "½ cup (60 g) ".
QUANTITY_BEFORE = re.compile(
    r"(?:\d|[¼½¾⅐-⅞]|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|half)\b)"
    r"\s*(?:[a-zA-Z.]+\s*){0,2}(?:\([^)]*\)\s*)?(?:of\s+)?(?:the\s+)?$",
    re.IGNORECASE,
)


class _Mention(NamedTuple):
    step: int
    start: int
    end: int
    ingredient_id: str
    words: str
    raw: Optional[str]
    before: str


def _mentions(directions: Sequence[TokenizedDirection]) -> List[_Mention]:
    found: List[_Mention] = []
    for index, step in enumerate(directions):
        for match in TOKEN_RE.finditer(step.text):
            before = strip_fat_tokens(step.text[: match.start()])
            found.append(_Mention(index, match.start(), match.end(), match.group(1), match.group(2),
                                  match.group(3), before))
    return found


def check_mentions(
    ingredients: Sequence[StructuredIngredient],
    directions: Sequence[TokenizedDirection],
) -> Tuple[List[TokenizedDirection], List[str]]:
    """
    Correct the uses the text and the arithmetic cannot support, so no mention claims a number it
    cannot have. Returns the directions, rewritten where needed, and one line per change. In order:

    - an amount that does not parse becomes ``none``;
    - an amount on words that are not a quantity — the model put it on the ingredient's name —
      becomes ``none`` when the text before already states a quantity ("1 tablespoon of the
      {{oil}}"), ``all`` when it is the line's whole amount ("Place the {{butter}}", 175 g of 175 g),
      and ``none`` otherwise;
    - every part (and remainder) of an ingredient whose parts, in its own unit, add up to more
      than the line gives becomes ``none`` — one of them is wrong, and nothing says which;
    - ``all`` beside a part or a remainder of the same ingredient becomes ``none``;
    - only the first ``all`` of an ingredient keeps it: "wash the rice … drain the rice" is one
      cup of rice, and printing it at every mention is noise.

    A legacy token is left alone: the client has its own rule for those. The client applies these
    same checks again (direction amounts design, D10); this is the import's half, and it logs.
    """
    by_id = {ing.id: ing for ing in ingredients}
    mentions = _mentions(directions)
    changes: List[str] = []
    effective: List[Tuple[Use, Optional[str]]] = []  # the use, and the reason it changed

    for m in mentions:
        use = parse_use(m.raw)
        ing = by_id.get(m.ingredient_id)
        if use.kind == "invalid":
            effective.append((Use("none"), f"amount {m.raw!r} does not parse"))
        elif use.kind == "part" and not QUANTITY_WORDS.search(m.words):
            whole = (
                ing is not None and ing.amount is not None and _unit_key(use.unit) == _unit_key(ing.unit)
                and abs((use.amount or 0) - ing.amount) <= ing.amount * (_PART_SLACK - 1)
            )
            if QUANTITY_BEFORE.search(m.before):
                effective.append((Use("none"), "an amount on the name, and the text already states one"))
            elif whole:
                effective.append((Use("all"), "an amount on the name that is the line's whole amount"))
            else:
                effective.append((Use("none"), "an amount on the name, not on a quantity"))
        else:
            effective.append((use, None))

    over: Set[str] = set()
    for ing_id, ing in by_id.items():
        parts = [u for (u, _), m in zip(effective, mentions) if m.ingredient_id == ing_id and u.kind == "part"]
        if ing.amount is None or not parts:
            continue
        if all(_unit_key(p.unit) == _unit_key(ing.unit) for p in parts):
            if sum(p.amount or 0 for p in parts) > ing.amount * _PART_SLACK:
                over.add(ing_id)
    shared = {
        m.ingredient_id for (u, _), m in zip(effective, mentions) if u.kind in ("part", "rest")
    }

    seen_all: Set[str] = set()
    final: List[Optional[str]] = []
    for (use, reason), m in zip(effective, mentions):
        kind = use.kind
        if kind in ("part", "rest") and m.ingredient_id in over:
            kind, reason = "none", "its parts add up to more than the ingredient line"
        elif kind == "all" and m.ingredient_id in shared:
            kind, reason = "none", "'all' beside a part of the same ingredient"
        elif kind == "all" and m.ingredient_id in seen_all:
            kind, reason = "none", "the whole amount was already shown at an earlier mention"
        if kind == "all":
            seen_all.add(m.ingredient_id)
        if reason is not None:
            changes.append(f"{m.ingredient_id} {m.words!r}: {reason}")
        final.append(m.raw if reason is None else kind)

    out: List[TokenizedDirection] = []
    cursor = 0
    for index, d in enumerate(directions):
        pieces: List[str] = []
        last = 0
        for m in mentions[cursor:]:
            if m.step != index:
                break
            pieces.append(d.text[last:m.start])
            chosen = final[cursor]
            keep = chosen == m.raw
            pieces.append(d.text[m.start:m.end] if keep else _token(m.ingredient_id, m.words, chosen or "none"))
            last = m.end
            cursor += 1
        pieces.append(d.text[last:])
        out.append(TokenizedDirection(step=d.step, text="".join(pieces)))
    return out, changes


_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00a0": " "})


def _fold(text: str) -> str:
    """One-for-one character folding, so positions in the folded text are positions in the text."""
    return text.translate(_FOLD)


def _locate(text: str, quote: str, context: str) -> List[int]:
    """
    Where ``quote`` may start in ``text``: the context's occurrences first, each giving the quote's
    place inside it; failing that, the quote's own occurrences. Curly quotes and dashes are folded
    on both sides, so "they’ll" in the step matches "they'll" in the model's context.
    """
    folded, q, c = _fold(text), _fold(quote), _fold(context)
    if c and q in c:
        offset = c.index(q)
        starts = [i + offset for i in _all(folded, c)]
        if starts:
            return starts
    return _all(folded, q)


def _all(text: str, needle: str) -> List[int]:
    found, at = [], text.find(needle)
    while needle and at >= 0:
        found.append(at)
        at = text.find(needle, at + 1)
    return found


def splice_mentions(
    steps: Sequence[str],
    mentions: Sequence[DirectionMention],
    valid_ids: Sequence[str],
    step_numbers: Optional[Sequence[int]] = None,
) -> Tuple[List[TokenizedDirection], List[str]]:
    """
    Tokenized directions built from step texts and the mentions a model found in them. The text is
    the steps', always: a token is placed only where its quote is found verbatim, so a model that
    misquotes loses that chip, never a word of the recipe. Each mention is placed on its own — by
    its context, else by an unambiguous quote — and the placements are then taken in text order,
    so a model that lists mentions out of order or twice loses nothing; a repeat of a placement
    already taken, or one that would overlap it, is dropped. Returns the directions and one line
    per mention that could not be placed. ``step_numbers`` keeps a recipe's own numbering.
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
        placed: List[Tuple[int, int, DirectionMention]] = []
        taken: Set[int] = set()
        for m in by_step.get(index + 1, []):
            candidates = _locate(text, m.quote, m.context)
            starts = [s for s in candidates if s not in taken]
            if candidates and not starts:
                continue  # the model listed a mention it had already listed
            # One candidate is a placement; several are a guess, unless the context named them.
            if len(starts) != 1 and not (starts and m.context and _fold(m.quote) in _fold(m.context)
                                         and _fold(m.context) in _fold(text)):
                dropped.append(f"step {m.step}: {m.quote!r} ({m.context!r}) not placed")
                continue
            start = starts[0]
            end = start + len(m.quote)
            if any(start < e and s < end for s, e, _ in placed):
                dropped.append(f"step {m.step}: {m.quote!r} overlaps another mention")
                continue
            taken.add(start)
            placed.append((start, end, m))
        placed.sort(key=lambda p: p[0])
        pieces: List[str] = []
        cursor = 0
        for start, end, m in placed:
            use = m.use.strip()
            pieces.append(text[cursor:start])
            pieces.append(_token(m.ingredient_id, text[start:end], use.lower() if use.lower() in _KEYWORDS else use))
            cursor = end
        pieces.append(text[cursor:])
        number = step_numbers[index] if step_numbers is not None else index + 1
        out.append(TokenizedDirection(step=number, text="".join(pieces)))
    return out, dropped


def is_tagged(directions: Sequence[TokenizedDirection]) -> bool:
    """Whether the directions carry tokens and every one says its use: written by 9.5.0 or later."""
    found = [match.group(3) for d in directions for match in TOKEN_RE.finditer(d.text)]
    return bool(found) and all(use is not None for use in found)
