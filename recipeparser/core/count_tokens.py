"""
recipeparser/core/count_tokens.py — counts in the method that must scale with the recipe.

Cayenne's scaling text audit (2026-10-24, F-491 and F-492) found two kinds of count the method
writes that stayed put when the cook scaled the recipe:

- a count of an ingredient, often as a word: "all four of the eggs" at 1.5 times still said four
  while the list said 6. The count is wrapped as an amount token, ``all {{ing_04|four|4}} of the
  {{ing_04|eggs|none}}``, so the client scales it with the list (and says it back as a word);
- a count of what the recipe makes: "Cut into 16 squares" at 24 servings. The count is wrapped as a
  yield token, ``Cut into {{yield:16|16}} squares``, which the client scales with the servings.

Both are deterministic and conservative: the rule is a missing number is safe and a wrong one is
not. A count of an ingredient is wrapped only when the ingredient is counted (no unit, a size word,
or the food itself as its unit), the count is its whole amount ("all four of the eggs" of 4) or a
share of it ("2 of the eggs" of 3), and the text does not make it a rate ("at a time", "each").
A yield count is wrapped only after a verb that cuts, divides or makes, before a word for the
pieces, and only when the number is the recipe's own yield (its servings or a number in its yield
text), so "cut each orange into 6 slices" and "cut the butter into 3 pieces" are left alone.

The words are never changed: stripping the tokens gives the step back as written. Running the pass
twice changes nothing. Pure: no I/O.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from recipeparser.core.fat_tokens import TOKEN_RE, check_mentions, is_special_id, parse_use, yield_token
from recipeparser.models import StructuredIngredient, TokenizedDirection

log = logging.getLogger(__name__)

_ONES = [
    "",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _word(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])


#: Every number word from one to ninety-nine (Cayenne's domain/countWords.ts spells the same).
WORDS = {_word(n): n for n in range(1, 100)}
_NUMBER_WORDS = "|".join(sorted(WORDS, key=len, reverse=True))
_COUNT = rf"(?:\d{{1,3}}|(?:a|\d{{1,2}}|{_NUMBER_WORDS})\s+dozen|{_NUMBER_WORDS})"


def count_value(words: str) -> Optional[int]:
    """The number a count's words stand for: "16", "sixteen", "a dozen", "two dozen"; None otherwise."""
    w = words.strip().lower()
    if w.isdigit():
        return int(w)
    m = re.fullmatch(r"(a|\d{1,2}|[a-z-]+)\s+dozen", w)
    if m:
        head = m.group(1)
        n = 1 if head == "a" else int(head) if head.isdigit() else WORDS.get(head)
        return None if n is None else n * 12
    return WORDS.get(w)


# A size is never a yield: the 9 of "9-inch", the 2 of 2".
_SIZE_AFTER = re.compile(
    r"""^\s*(?:-\s*)?(?:inch|inches|in\b|in\.|cm|mm|"|”|″|x\b|×|by\b|lb|kg|g\b|oz)""", re.IGNORECASE
)


def yield_numbers(
    base: Optional[float], servings_min: Optional[int], servings_max: Optional[int], servings_note: Optional[str]
) -> Set[int]:
    """The numbers that are the recipe's yield: its servings, and each count its yield text gives."""
    found: Set[int] = set()
    for raw in (base, servings_min, servings_max):
        try:  # PostgREST may send a numeric column as a string
            n = float(raw) if raw is not None and not isinstance(raw, bool) else None
        except (TypeError, ValueError):
            n = None
        if n is not None and n >= 1 and n.is_integer():
            found.add(int(n))
    note = servings_note or ""
    for m in re.finditer(rf"(?<![\w/.,])({_COUNT})(?![\w/.,])", note, re.IGNORECASE):
        if _SIZE_AFTER.match(note[m.end() :]):
            continue
        n = count_value(m.group(1))
        if n is not None and n >= 1:
            found.add(n)
    return found


# What a method cuts, divides or shapes the bake into. "Pieces" is here, but only a count that is
# the recipe's own yield is ever wrapped, so "cut the butter into 3 pieces" is not.
_PIECES = (
    "squares|bars|pieces|slices|wedges|portions|servings|cookies|biscuits|muffins|cupcakes|rolls|buns|balls|"
    "rounds|discs|disks|triangles|rectangles|fingers|tarts|tartlets|pies|scones|pastries|pasties|dumplings|"
    "brownies|blondies|doughnuts|donuts|bagels|pretzels|crackers|empanadas|meringues|truffles|macarons|"
    "cakes|loaves|logs|patties|rings|strips|flapjacks|shortcakes|biscotti|portions"
)
_VERB = (
    r"\b(?:cut|cuts|slice|divide|portion|makes?|yields?|shape|form|roll|cut\s+out|stamp\s+out|"
    r"you\s+should\s+(?:get|have)|you(?:'ll|’ll|\s+will)\s+(?:get|have)|to\s+(?:get|make))\b"
)
# Up to two words between the number and the pieces: an adjective, never a rate.
_ADJ = (
    r"(?:(?:even|equal|equally|large|small|medium|generous|neat|roughly|approximately|about|thin|thick|"
    r"individual|mini|bite-sized?|long|square|round|rectangular|triangular|uniform|evenly\s+sized|"
    r"same-sized?)\s+){0,2}"
)
# A size between the number and the pieces: "sixteen 2 inch bars", "12 (2-inch) squares".
_DIM = (
    r"(?:\d+(?:[.,/]\d+)?\s*(?:-|\s)?(?:inch(?:es)?|in\.?|cm|\"|”|-inch)(?:[\s-](?:by|x|×)[\s-]\d+(?:[.,/]\d+)?"
    r"\s*(?:-|\s)?(?:inch(?:es)?|in\.?|cm|\"|”)?)?[\s-]+|\(\s*[^)]{1,20}\)\s+)?"
)
_YIELD_RE = re.compile(
    rf"{_VERB}(?P<gap>[^.;:!?]{{0,40}}?)(?<![\w/.,])(?P<n1>{_COUNT})"
    rf"(?:(?P<sep>\s*(?:-|–|to|or)\s*)(?P<n2>{_COUNT}))?\s+{_DIM}{_ADJ}(?:{_PIECES})\b",
    re.IGNORECASE,
)
_EACH = re.compile(r"\b(?:each|every|per)\b", re.IGNORECASE)


def _token_spans(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def _inside(spans: Sequence[Tuple[int, int]], start: int, end: int) -> bool:
    return any(s < end and start < e for s, e in spans)


def _plain(text: str) -> Tuple[str, List[int], List[bool]]:
    """The step as written (every token as its words), and for each character its place in ``text``
    and whether it lies inside a token."""
    where: List[int] = []
    tokened: List[bool] = []
    cursor = 0
    for m in TOKEN_RE.finditer(text):
        where += range(cursor, m.start())
        tokened += [False] * (m.start() - cursor)
        where += range(m.start(2), m.end(2))
        tokened += [True] * (m.end(2) - m.start(2))
        cursor = m.end()
    where += range(cursor, len(text))
    tokened += [False] * (len(text) - cursor)
    return "".join(text[i] for i in where), where, tokened


def _tag_yields(text: str, yields: Set[int], counted_words: Set[str]) -> Tuple[str, List[str]]:
    if not yields:
        return text, []
    plain, where, tokened = _plain(text)
    wraps: List[Tuple[int, int, int]] = []  # start, end in text, value
    lines: List[str] = []
    for m in _YIELD_RE.finditer(plain):
        gap = m.group("gap")
        if _EACH.search(gap):
            continue  # "cut each half into 6 slices": a rate per piece, never the yield
        if re.search(r"\d|[¼½¾⅐-⅞]", gap) or counted_words & {_singular(w) for w in re.findall(r"[a-zA-Z]+", gap)}:
            continue  # "cut the tomato into 4 wedges": an ingredient counted by the piece, not the bake
        groups = [g for g in ("n1", "n2") if m.group(g)]
        values = [count_value(m.group(g)) for g in groups]
        if not any(v in yields for v in values):
            continue
        for g, v in zip(groups, values):
            s, e = m.start(g), m.end(g)
            if v is None or v < 1 or any(tokened[s:e]):
                continue
            wraps.append((where[s], where[e - 1] + 1, v))
            lines.append(f"yield {v}: {m.group(0)!r}")
    return _apply(text, [(s, e, yield_token(v, text[s:e])) for s, e, v in wraps]), lines


_SIZE_WORDS = {"large", "medium", "small", "extra-large", "extra large", "jumbo", "whole", "big"}
# Words in an ingredient's name that say nothing about which food it is.
_NOT_FOOD = {
    "large",
    "medium",
    "small",
    "whole",
    "fresh",
    "ripe",
    "free",
    "range",
    "the",
    "and",
    "for",
    "with",
    "into",
    "dough",
    "pastry",
    "batter",
    "mixture",
}


def _singular(word: str) -> str:
    w = word.lower().rstrip(".")
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("oes"):
        return w[:-2]
    return w[:-1] if w.endswith("s") else w


def _counted(ing: StructuredIngredient) -> bool:
    """An ingredient measured by the piece: no unit, a size word, or the food itself as its unit ("3 bananas")."""
    if ing.amount is None or ing.amount <= 0:
        return False
    if ing.unit is None or ing.unit.strip().lower() in _SIZE_WORDS:
        return True
    name = {_singular(w) for w in re.findall(r"[a-zA-Z]+", ing.name)}
    return _singular(ing.unit.strip()) in name


# The count right before a mention: "all four of the ", "Separate 2 of the ", "approximately 3 ".
_COUNT_BEFORE = re.compile(
    rf"(?<![\w/.,])(?P<n>{_COUNT})(?P<gap>\s+(?:(?P<of>of)\s+)?(?:(?:the|your)\s+)?(?:(?:large|medium|small|whole)\s+)?)$",
    re.IGNORECASE,
)
# A count that is one end of a range or one choice of two: "one or two eggs", "2 to 3 apples".
_RANGE_BEFORE = re.compile(r"(?:\bor|\bto|-|–)\s*$", re.IGNORECASE)
# A rate: "two eggs at a time", "one apple each", "4 per tart".
_RATE_AFTER = re.compile(r"^[^.;,]{0,25}?\b(?:at\s+a\s+time|each|per|apiece|a\s+piece)\b", re.IGNORECASE)


def _tag_ingredient_counts(text: str, by_id: dict) -> Tuple[str, List[str]]:
    spans = _token_spans(text)
    wraps: List[Tuple[int, int, str]] = []
    lines: List[str] = []
    for i, m in enumerate(TOKEN_RE.finditer(text)):
        if is_special_id(m.group(1)) or parse_use(m.group(3)).kind not in ("none", "all", "rest", "legacy"):
            continue
        ing = by_id.get(m.group(1))
        if ing is None or not _counted(ing):
            continue
        region_start = spans[i - 1][1] if i > 0 else 0
        before = text[region_start : m.start()]
        c = _COUNT_BEFORE.search(before)
        if not c:
            continue
        n = count_value(c.group("n"))
        if n is None or n < 1 or _RANGE_BEFORE.search(before[: c.start()]) or _RATE_AFTER.match(text[m.end() :]):
            continue
        whole = abs(n - ing.amount) < 1e-9
        share = n < ing.amount and c.group("of") is not None
        if not (whole or share):
            continue
        start = region_start + c.start("n")
        end = region_start + c.end("n")
        if _inside(spans, start, end):
            continue
        wraps.append((start, end, "{{" + ing.id + "|" + text[start:end] + "|" + str(n) + "}}"))
        lines.append(f"{ing.id} count {n}: {text[start : m.end()]!r}")
    return _apply(text, wraps), lines


def _apply(text: str, wraps: Iterable[Tuple[int, int, str]]) -> str:
    out = text
    for start, end, token in sorted(wraps, key=lambda w: w[0], reverse=True):
        out = out[:start] + token + out[end:]
    return out


def scalable_counts(
    directions: Sequence[TokenizedDirection],
    ingredients: Sequence[StructuredIngredient],
    yields: Set[int],
) -> List[TokenizedDirection]:
    """``tag_counts``, then the import's checks again (a count now carrying the number makes an ``all``
    elsewhere give way), each change logged. What ASSEMBLE and the regeneration worker write."""
    tagged, lines = tag_counts(directions, ingredients, yields)
    checked, demotions = check_mentions(ingredients, tagged)
    for line in lines:
        log.info("count_tokens: %s", line)
    for line in demotions:
        log.info("count_tokens: mention demoted to none: %s", line)
    return checked


def tag_counts(
    directions: Sequence[TokenizedDirection],
    ingredients: Sequence[StructuredIngredient],
    yields: Set[int],
) -> Tuple[List[TokenizedDirection], List[str]]:
    """The directions with each scalable count wrapped, and one line per count wrapped."""
    by_id = {ing.id: ing for ing in ingredients if ing.kind == "ingredient"}
    # The words of every ingredient counted by the piece ("tomato", "lemon"): cutting one is not the bake.
    counted_words = {
        _singular(w) for ing in by_id.values() if _counted(ing) for w in re.findall(r"[a-zA-Z]{3,}", ing.name)
    } - _NOT_FOOD
    out: List[TokenizedDirection] = []
    lines: List[str] = []
    for d in directions:
        text, counted = _tag_ingredient_counts(d.text, by_id)
        text, yielded = _tag_yields(text, yields, counted_words)
        lines += [f"step {d.step}: {line}" for line in counted + yielded]
        out.append(d if text == d.text else d.model_copy(update={"text": text}))
    return out, lines
