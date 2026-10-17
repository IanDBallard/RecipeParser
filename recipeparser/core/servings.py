"""Classify a recipe's yield text as stated, counted, the bake itself, or none.

Pure and deterministic: no I/O, no model. The shared fixture
``tests/fixtures/servings_source_cases.json`` is the contract; the Cayenne
client runs a TypeScript twin against a verbatim copy of it (D44, D47, D50).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from recipeparser.core.durations import Span, _normalise, parse_servings

SERVING_WORDS_RE = re.compile(r"\b(?:serves|servings?|people|persons?|portions?)\b")

ITEM_NOUNS = (
    "cookies", "biscuits", "muffins", "cupcakes", "scones", "buns", "rolls",
    "bagels", "pretzels", "crackers", "bars", "squares", "brownies",
    "blondies", "slices", "pieces", "tarts", "tartlets", "pies", "pancakes",
    "crêpes", "crepes", "waffles", "doughnuts", "donuts", "macarons",
    "meringues", "truffles", "balls", "dumplings", "pasties", "empanadas",
    "sausage rolls",
)

# "volumes and containers" (builder-check item 8) fall to the model as well.
BAKE_NOUNS = (
    "cake", "cakes", "loaf", "loaves", "pan", "pans", "tin", "tins", "pie",
    "tart", "pizza", "pizzas", "cheesecake", "bundt", "ring", "sheet", "tray",
    "traybake", "dish", "batch", "quart", "quarts", "cup", "cups", "litre",
    "litres", "liter", "liters", "jar", "jars", "pint", "pints",
)

_METHOD_COUNT_RES = (
    re.compile(
        r"\b(?:cut|slice|divide|portion)\b[^.;]{0,40}?\binto\b\s+(\d{1,3})\b"
        r"(?!\s*[-–]?\s*(?:inch|in\b|cm|mm|centimet|millimet|\"))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:makes|yields)\s+(\d{1,3})\b"
        r"(?!\s*(?:cup|cups|quart|litre|liter|ml|g\b|kg|oz|lb))",
        re.IGNORECASE,
    ),
)


def _singular(noun: str) -> Optional[str]:
    """The singular of a plural item noun, or None when it is ambiguous."""
    if noun == "pies":  # plural only: "pie" is a bake noun
        return None
    if noun.endswith("ies"):
        one = noun[:-3] + "y"
    elif noun.endswith("ves"):
        one = noun[:-3] + "f"
    elif noun.endswith("s"):
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


@dataclass(frozen=True)
class Classified:
    source: Literal["stated", "counted", "bake", "none"]
    base: Optional[int]
    span: Span


def classify_servings(
    servings_text: Optional[str], directions: Sequence[str]
) -> Classified:
    span = parse_servings(servings_text)
    text = _normalise(servings_text or "")
    if span.min is not None:
        if SERVING_WORDS_RE.search(text):
            return Classified("stated", span.min, span)
        note = span.note or ""
        if _ITEM_RE.search(note) or (
            span.min == 1 and _ITEM_SINGULAR_RE.search(note)
        ):
            return Classified("counted", span.min, span)
        if _BAKE_RE.search(note):
            return Classified("bake", None, span)
        return Classified("stated", span.min, span)
    for step in directions:
        for pattern in _METHOD_COUNT_RES:
            m = pattern.search(step)
            if m and int(m.group(1)) >= 2:
                return Classified("counted", int(m.group(1)), span)
    if text:
        return Classified("bake", None, span)
    return Classified("none", None, span)
