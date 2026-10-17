"""Classify a recipe's yield text as stated, counted, the bake itself, or none,
and compose that with the tin table and the model's estimate into base_servings
and its servings_source.

Pure and deterministic: no I/O, no model call (the model's estimate is passed in). The shared fixture
``tests/fixtures/servings_source_cases.json`` is the contract; the Cayenne
client runs a TypeScript twin against a verbatim copy of it (D44, D47, D50).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from recipeparser.core.durations import Span, _normalise, parse_servings
from recipeparser.core.tin_servings import estimate_from_tins
from recipeparser.models import RecipePan, ServingsSource

SERVING_WORDS_RE = re.compile(r"\b(?:serves|servings?|people|persons?|portions?)\b")

ITEM_NOUNS = (
    "cookies", "biscuits", "muffins", "cupcakes", "scones", "buns", "rolls",
    "bagels", "pretzels", "crackers", "bars", "squares", "brownies",
    "blondies", "slices", "wedges", "pieces", "tarts", "tartlets", "pies", "pancakes",
    "crêpes", "crepes", "waffles", "doughnuts", "donuts", "macarons",
    "meringues", "truffles", "balls", "dumplings", "pasties", "empanadas",
    "sausage rolls",
)

# "volumes and containers" (builder-check item 8) fall to the model as well.
BAKE_NOUNS = (
    "cake", "cakes", "loaf", "loaves", "pan", "pans", "tin", "tins", "pie",
    "tart", "pizza", "pizzas", "cheesecake", "bundt", "ring", "sheet", "tray",
    "traybake", "dish", "batch", "quart", "quarts", "cup", "cups", "litre",
    "litres", "liter", "liters", "jar", "jars", "pint", "pints", "swiss roll",
    "swiss rolls",
)

# What a method may cut or divide a bake into: the item nouns but "pieces" (butter and chicken
# are cut into pieces too), and the cuts that only a method names.
PORTION_NOUNS = tuple(n for n in ITEM_NOUNS if n != "pieces") + (
    "triangles", "fingers", "servings", "portions",
)


def _singular(noun: str) -> Optional[str]:
    """The singular of a plural item noun, or None when it is ambiguous."""
    if noun == "pies":  # plural only: "pie" is a bake noun
        return None
    if noun == "pasties":  # the only item noun whose singular is "-y"
        one = "pasty"
    elif noun.endswith("s"):  # cookies -> cookie, brownies -> brownie
        one = noun[:-1]
    else:
        one = noun
    return None if one in BAKE_NOUNS else one


def _alternation(words: Sequence[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_ITEM_RE = re.compile(rf"\b(?:{_alternation(ITEM_NOUNS)})\b")
_ITEM_SINGULAR_RE = re.compile(
    rf"\b(?:{_alternation([s for s in map(_singular, ITEM_NOUNS) if s])})\b"
)
_BAKE_RE = re.compile(rf"\b(?:{_alternation(BAKE_NOUNS)})\b")

# One optional word between a number and its noun ("4 generous portions", "24 small pancakes"),
# never a dimension ("3 x 2 inch bars", "2 cm pieces").
_ADJ = r"(?:(?!(?:x|by|inch|inches|in|cm|mm)\b)[a-z]+\s+)?"
_RANGE = r"(?:\s*-\s*\d+|\s+to\s+\d+)?"
_APPROX = r"(?:(?:about|approx\.?|approximately|roughly|around|up to)\s+)?"

# The number adjacent to a serving word (C2a): "serves 10", "servings: 4", "8-10 servings".
_SERVES_RES = (
    re.compile(rf"\bserves\b\s*:?\s*{_APPROX}(\d+)"),
    re.compile(rf"\b(?:servings?|portions?)\b\s*:?\s*{_APPROX}(\d+)"),
    re.compile(rf"\b(\d+){_RANGE}\s+{_ADJ}(?:servings?|people|persons?|portions?)\b"),
)
# Never a count of a bake noun that ends in an item noun: "2 swiss rolls".
_NOT_BAKE = rf"(?!(?:{_alternation(BAKE_NOUNS)})\b)"
# The number attached to an item noun, anywhere in the yield (C2c): "2 loaves (24 slices)".
_COUNT_RE = re.compile(rf"\b(\d+){_RANGE}(\s+dozen)?\s+{_NOT_BAKE}{_ADJ}(?:{_alternation(ITEM_NOUNS)})\b")
_A_DOZEN_COUNT_RE = re.compile(rf"\ba\s+dozen\s+{_NOT_BAKE}{_ADJ}(?:{_alternation(ITEM_NOUNS)})\b")
# "N dozen" or "a dozen" with no number parse_servings could read: "Makes about 2 dozen".
_DOZEN_ANY_RE = re.compile(r"\b(\d+|a)\s+dozen\b")
# A note that is a separate phrase after the number (C1): "12 one 9-inch pan", "8 (one 20 cm cake)",
# "8 (16 slices)", "6 ; 12 cookies" -- the number is servings even when the phrase holds a count.
_SEPARATE_RE = re.compile(r"^(?:[(,;]|(?:one|a|an)\s)")
# A phrase that holds only a bare number, the trace of a stripped serving word ("12 ( 6)",
# "24 cookies ( 12)", "1 pie, 8"), or a second number straight after the first ("12 24 bars"):
# which number serves is lost, so the yield is the bake (the tins or the model place it).
_BARE_NUMBER_RE = re.compile(r"(?:^|[(,;])\s*\d+(?:\s*(?:-|to)\s*\d+)?\s*(?=\)|[,;]|$)|^\d+\s")
# ...but not a parenthesised size of the bake the number counts: "2 (9-inch) loaves".
_PAREN_SIZE_RE = re.compile(rf"^\([^)]*\)\s*(?:{_alternation(BAKE_NOUNS)})\b")
_LEADING_APPROX_RE = re.compile(r"^(?:(?:about|approx\.?|approximately|roughly|around)\s+)+")
_DOZEN_RE = re.compile(r"^dozen\b\s*")

_METHOD_COUNT_RES = (
    re.compile(
        rf"\b(?:cut|slice|divide|portion)\b[^.;]{{0,40}}?\binto\b\s+(\d{{1,3}})(\s+dozen)?\s+{_NOT_BAKE}{_ADJ}"
        rf"(?:{_alternation(PORTION_NOUNS)})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:makes|yields)\s+{_APPROX}(\d{{1,3}})(\s+dozen)?\s+{_NOT_BAKE}{_ADJ}"
        rf"(?:{_alternation(PORTION_NOUNS)})\b",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class Classified:
    source: Literal["stated", "counted", "bake", "none"]
    base: Optional[int]
    span: Span


def _first(matches: Sequence[Optional["re.Match[str]"]]) -> Optional["re.Match[str]"]:
    found = [m for m in matches if m is not None]
    return min(found, key=lambda m: m.start()) if found else None


def _stated_number(text: str) -> Optional[int]:
    """The first number of one or more adjacent to a serving word, or None."""
    m = _first([next((m for m in r.finditer(text) if int(m.group(1)) >= 1), None) for r in _SERVES_RES])
    return int(m.group(1)) if m else None


def _counted_number(text: str) -> Optional[int]:
    """The first count of one or more attached to an item noun ("2 dozen cookies" is 24), or None."""
    for m in _COUNT_RE.finditer(text):
        n = int(m.group(1)) * (12 if m.group(2) else 1)
        if n >= 1:
            return n
    return None


def classify_servings(
    servings_text: Optional[str], directions: Sequence[str]
) -> Classified:
    span = parse_servings(servings_text)
    text = _normalise(servings_text or "")
    stated = _stated_number(text)
    if stated is not None:
        return Classified("stated", stated, span)
    if span.min is not None and span.min >= 1:  # below one is no number (M3)
        n = span.min
        note = _LEADING_APPROX_RE.sub("", span.note or "")
        if _DOZEN_RE.match(note):  # "about 3 dozen" is 36
            n, note = n * 12, _DOZEN_RE.sub("", note)
        if _BARE_NUMBER_RE.search(note):  # "12 ( 6)": which number serves is lost
            return Classified("bake", None, span)
        if not note or (_SEPARATE_RE.match(note) and not _PAREN_SIZE_RE.match(note)):
            return Classified("stated", n, span)
        counted = _counted_number(text)
        if counted is not None:
            return Classified("counted", counted, span)
        if _BAKE_RE.search(note):  # before the singular item noun: "1 swiss roll", "1 bar cake"
            return Classified("bake", None, span)
        if _ITEM_RE.search(note) or (n == 1 and _ITEM_SINGULAR_RE.search(note)):
            return Classified("counted", n, span)
        return Classified("stated", n, span)
    counted = _counted_number(text)
    if counted is not None:
        return Classified("counted", counted, span)
    if _A_DOZEN_COUNT_RE.search(text):  # "a dozen cookies"
        return Classified("counted", 12, span)
    dozen = _DOZEN_ANY_RE.search(text)
    if dozen and dozen.group(1) != "0":  # "Makes about 2 dozen", "Yield: 2 dozen"
        return Classified("stated", 12 * (1 if dozen.group(1) == "a" else int(dozen.group(1))), span)
    for step in directions:
        for pattern in _METHOD_COUNT_RES:
            m = pattern.search(step)
            if m and int(m.group(1)) >= 2:
                return Classified("counted", int(m.group(1)) * (12 if m.group(2) else 1), span)
    if text:
        return Classified("bake", None, span)
    return Classified("none", None, span)


@dataclass(frozen=True)
class Servings:
    base: Optional[int]
    source: Optional[ServingsSource]  # None when unknown


def text_from_span(span: Span) -> str:
    """The yield text a stored servings span stands for: "min-max note", "min note", the note, or ""."""
    if span.min is None:
        return span.note or ""
    number = str(span.min) if span.max is None or span.max == span.min else f"{span.min}-{span.max}"
    return f"{number} {span.note}" if span.note else number


def servings_for_recipe(
    servings_text: Optional[str],
    directions: Sequence[str],
    pans: Sequence[RecipePan],
    bake_type: Optional[str],
    model_estimate: Optional[int],
) -> Servings:
    """The recipe's words, then its tins, then the model's estimate; else nothing."""
    c = classify_servings(servings_text, directions)
    if c.source in ("stated", "counted"):
        return Servings(c.base, c.source)
    tin = estimate_from_tins(pans, bake_type)
    if tin is not None:
        return Servings(tin, "tin")
    if model_estimate is not None:
        return Servings(model_estimate, "estimated")
    return Servings(None, None)


def servings_for_restore(
    span: Span,
    directions: Sequence[str],
    pans: Sequence[RecipePan],
    bake_type: Optional[str],
    base: Optional[int],
) -> Servings:
    """
    servings_source for an archive that predates it, never asking the model (a restore makes no
    AI call). A yield the recipe states or counts keeps the exported base with no source: a
    pre-9.17 number reads as the recipe's own. Otherwise the tin table, else the base as exported.
    """
    c = classify_servings(text_from_span(span), directions)
    if c.source in ("stated", "counted"):
        return Servings(base, None)
    tin = estimate_from_tins(pans, bake_type)
    if tin is not None:
        return Servings(tin, "tin")
    return Servings(base, None)
