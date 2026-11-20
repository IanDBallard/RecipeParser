"""
recipeparser/core/durations.py — deterministic duration and servings parsing.

Spec 3.6: prep/cook times and servings are stored as min/max integers plus a
free-text note and displayed as "x to y".  This module is the Python half of
a two-implementation parser; the TypeScript half in Cayenne runs the same
fixture (tests/fixtures/duration_cases.json).  Change the rules here and there
together, and add the case to the fixture first.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

_FRACTIONS = {
    "½": " 1/2", "¼": " 1/4", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3",
    "⅛": " 1/8", "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8",
}
_NUM = r"(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
_UNIT = r"(hours?|hrs?|h|minutes?|mins?|m|days?|d)"
_UNIT_MINUTES = {
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "d": 1440, "day": 1440, "days": 1440,
}
_GROUP_RE = re.compile(rf"{_NUM}\s*{_UNIT}?")
_PURE_NUM_RE = re.compile(rf"^{_NUM}$")
_QUALIFIER_RE = re.compile(r"\s*(?:\bplus\b|\+|,|\()")
_RANGE_RE = re.compile(r"\s*(?:-|\bto\b)\s*")
_SERVING_WORDS_RE = re.compile(r"\b(serves|servings?|portions?|makes|yields?|people|persons?)\b")
_SERVING_RANGE_RE = re.compile(rf"^{_NUM}(?:\s*(?:-|\bto\b)\s*{_NUM})?")
_APPROX_RE = re.compile(r"^(about|approx\.?|approximately|roughly)\s+")


@dataclass(frozen=True)
class Span:
    min: Optional[int]
    max: Optional[int]
    note: Optional[str]


_EMPTY = Span(None, None, None)


def _number(token: str) -> float:
    token = token.strip()
    if " " in token:                      # mixed fraction "1 1/2"
        whole, frac = token.split(None, 1)
        return float(whole) + _number(frac)
    if "/" in token:
        num, den = token.split("/", 1)
        return float(num) / float(den)
    return float(token)


def _normalise(text: str) -> str:
    for glyph, ascii_ in _FRACTIONS.items():
        text = text.replace(glyph, ascii_)
    text = text.lower().replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def _split_qualifier(text: str) -> Tuple[str, Optional[str]]:
    """'45 min plus chilling' -> ('45 min', 'plus chilling')."""
    m = _QUALIFIER_RE.search(text)
    if not m:
        return text, None
    head = text[: m.start()].strip()
    tail = text[m.start():].strip().lstrip(",(").rstrip(")").strip()
    if tail.startswith("+"):
        tail = "plus " + tail[1:].strip()
    return head, (tail or None)


def _side_minutes(side: str) -> Optional[float]:
    """Minutes for one side of a range, or None if anything is unparseable."""
    side = side.strip()
    if not side:
        return None
    total = 0.0
    last_unit: Optional[str] = None
    consumed = 0
    for m in _GROUP_RE.finditer(side):
        if side[consumed: m.start()].strip():
            return None                   # stray text between groups
        n = _number(m.group(1))
        unit = m.group(2)
        if unit:
            total += n * _UNIT_MINUTES[unit]
            last_unit = unit
        elif last_unit is None:
            total += n                    # bare number: minutes
        else:
            total += n                    # "1 hr 30" -> trailing minutes
        consumed = m.end()
    if consumed == 0 or side[consumed:].strip():
        return None
    return total


def parse_duration(text: Optional[str]) -> Span:
    if text is None or not text.strip():
        return _EMPTY
    original = text.strip()
    head, note = _split_qualifier(_normalise(original))
    parts = _RANGE_RE.split(head, maxsplit=1)
    if len(parts) == 2:
        lo_text, hi_text = parts
        hi = _side_minutes(hi_text)
        if _PURE_NUM_RE.match(lo_text.strip()) and hi is not None:
            unit_m = re.search(_UNIT, hi_text)
            factor = _UNIT_MINUTES[unit_m.group(1)] if unit_m else 1
            lo: Optional[float] = _number(lo_text) * factor
        else:
            lo = _side_minutes(lo_text)
    else:
        lo = hi = _side_minutes(head)
    if lo is None or hi is None:
        # Tidied, not normalised: this note is shown to a cook, so it keeps its
        # case and glyphs -- but a source recipe whose duration field holds a
        # number followed by thousands of newlines must not carry them into
        # cook_note, which syncs to every device.
        return Span(None, None, re.sub(r"\s+", " ", original).strip())
    return Span(int(round(lo)), int(round(hi)), note)


def parse_servings(text: Optional[str]) -> Span:
    if text is None or not text.strip():
        return _EMPTY
    original = text.strip()
    norm = _normalise(original)
    approx: Optional[str] = None
    m = _APPROX_RE.match(norm)
    if m:
        approx = m.group(1)
        norm = norm[m.end():]
    cleaned = re.sub(r"\s+", " ", _SERVING_WORDS_RE.sub(" ", norm)).strip()
    m = _SERVING_RANGE_RE.match(cleaned)
    if not m:
        # Same tidy as parse_duration's fallback, for the same reason.
        return Span(None, None, re.sub(r"\s+", " ", original).strip())
    lo = _number(m.group(1))
    hi = _number(m.group(2)) if m.group(2) else lo
    rest = cleaned[m.end():].strip(" ,")
    note = " ".join(p for p in (approx, rest) if p) or None
    return Span(int(round(lo)), int(round(hi)), note)


def duration_columns(
    prep_text: Optional[str],
    cook_text: Optional[str],
    servings_text: Optional[str],
    fallback_base_servings: Optional[int],
) -> Dict[str, Any]:
    """The nine structured columns plus base_servings, for a writer or backfill."""
    p, c, s = parse_duration(prep_text), parse_duration(cook_text), parse_servings(servings_text)
    return {
        "prep_min_minutes": p.min, "prep_max_minutes": p.max, "prep_note": p.note,
        "cook_min_minutes": c.min, "cook_max_minutes": c.max, "cook_note": c.note,
        "servings_min": s.min, "servings_max": s.max, "servings_note": s.note,
        "base_servings": s.min if s.min is not None else fallback_base_servings,
    }


# ---------------------------------------------------------------------------
# A time written as steps (Cayenne Fix Roadmap F-688)
#
# Paprika's prep and cook fields are free text, and some recipes list every step:
# "2 minutes (fry baris) + 4 minutes (fry onions) + 30 minutes (simmer)". parse_duration
# reads the first figure and calls the rest a qualifier, so that recipe cooks in 2 minutes.
# This is not part of the shared parse_duration contract (Cayenne's duration.ts has no twin):
# a reader rewrites such a field to "<total> (<as written>)" before it is stored, and both
# parsers read that as the total with the writer's words as the note.
# ---------------------------------------------------------------------------

_PARENS_RE = re.compile(r"\([^()]*\)")
_STEP_SPLIT_RE = re.compile(r"\s*[+;,]\s*")
_STEP_PREFIX_RE = re.compile(
    r"^(?:(?:and\s+)?then\s+)?(?:a\s+further\s+|further\s+|another\s+)?"
    r"(?:about\s+|approx\.?\s+|approximately\s+|roughly\s+)?"
)
_STEP_RE = re.compile(
    rf"^((?:{_NUM}\s*{_UNIT}?\s*(?:-|\bto\b)\s*)?{_NUM}\s*{_UNIT}\b(?:\s*{_NUM}\s*{_UNIT}\b)*)\s*(?P<label>.*)$"
)
# A label word that makes a step something other than time added to the total: an
# alternative ("or"), a multiplier ("per batch", "each side"), a condition or an open end.
_STEP_LABEL_STOP_RE = re.compile(r"\b(or|per|each|if|plus|until|for|optional)\b|\d")
# The same, for a bracketed label: "(for stock)" names a step, "(or 20 minutes in a pressure
# cooker)" and "(per batch)" do not.
_PAREN_LABEL_STOP_RE = re.compile(r"\b(or|per|each|if|optional)\b|\d")


def _step_minutes(step: str) -> Optional[Tuple[int, int]]:
    step = _STEP_PREFIX_RE.sub("", step.strip())
    m = _STEP_RE.match(step)
    if not m or _STEP_LABEL_STOP_RE.search(m.group("label")):
        return None
    span = parse_duration(m.group(1))
    if span.min is None or span.max is None or span.note is not None:
        return None
    return span.min, span.max


def _minutes_text(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minutes"
    hours, rest = divmod(minutes, 60)
    text = f"{hours} hour" + ("s" if hours != 1 else "")
    return text + (f" {rest} minutes" if rest else "")


def summed_duration_text(text: Optional[str]) -> Optional[str]:
    """
    A time written as two or more steps, rewritten as "<total> (<as written>)": "1 hour (simmer
    pork); 30 minutes (simmer carrots)" -> "1 hour 30 minutes (1 hour (simmer pork); 30 minutes
    (simmer carrots))". Steps are joined by "+", ";", "," or "then"; each is a figure or a range
    with an optional label. None when the text is not such a sum: one figure, an alternative
    ("2 hours or 30 minutes"), a qualifier ("10 mins, plus making the sauce"), a multiplier
    ("per batch") or a step with no figure ("then until tender").
    """
    if text is None or not text.strip():
        return None
    original = re.sub(r"\s+", " ", text).strip()
    norm = _normalise(original)
    while _PARENS_RE.search(norm):
        inner = [g[1:-1] for g in _PARENS_RE.findall(norm)]
        if any(_PAREN_LABEL_STOP_RE.search(g) for g in inner):
            return None
        norm = _PARENS_RE.sub(" ", norm)
    if "(" in norm or ")" in norm:
        return None
    steps = _STEP_SPLIT_RE.split(re.sub(r"\s+", " ", norm).strip())
    if len(steps) < 2:
        return None
    lo = hi = 0
    for step in steps:
        minutes = _step_minutes(step)
        if minutes is None:
            return None
        lo, hi = lo + minutes[0], hi + minutes[1]
    total = _minutes_text(lo) if lo == hi else f"{_minutes_text(lo)} to {_minutes_text(hi)}"
    return f"{total} ({original})"
