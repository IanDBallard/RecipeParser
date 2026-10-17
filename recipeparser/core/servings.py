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
