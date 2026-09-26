"""
recipeparser/core/numbers.py — the numbers a text writes, by value (verbatim-ingestion D2, D3).

Pure: no I/O, no imports from recipeparser.io or recipeparser.adapters. durations.py keeps its own
fraction table because Cayenne's TypeScript twin mirrors it; this one is separate so that neither
contract moves when the other does.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, List, Set

# Every vulgar-fraction glyph, with a leading space so "1½" reads "1 1/2" — the one rewrite the
# extract prompt permits (D1). The fraction slash (U+2044) becomes a plain "/".
_FRACTIONS = {
    "½": " 1/2", "⅓": " 1/3", "⅔": " 2/3", "¼": " 1/4", "¾": " 3/4",
    "⅕": " 1/5", "⅖": " 2/5", "⅗": " 3/5", "⅘": " 4/5", "⅙": " 1/6", "⅚": " 5/6",
    "⅐": " 1/7", "⅛": " 1/8", "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8", "⅑": " 1/9", "⅒": " 1/10",
}
_NUMBER = re.compile(r"\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?")
# "1,5 dl" is how a Swedish writer puts 1.5 (cookbook locales D6).
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")

# Old books spell their numbers ("HALF AN OUNCE", "A THIRD PART"), and D2 is a presence check by
# value, so a number word in the SOURCE states that value. The lines side still reads numerals only.
_CARDINAL_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "dozen": 12,
}
_FRACTION_WORDS = {
    "half": 1 / 2, "halves": 1 / 2, "third": 1 / 3, "thirds": 1 / 3,
    "quarter": 1 / 4, "quarters": 1 / 4, "fourth": 1 / 4, "fourths": 1 / 4,
    "eighth": 1 / 8, "eighths": 1 / 8,
}
_PLURAL_FRACTION_WORDS = {"halves", "thirds", "quarters", "fourths", "eighths"}
# Whole words only: "often" is one word, so it never yields "ten".
_WORD = re.compile(r"[a-z]+")


def _normalise(text: str) -> str:
    for glyph, ascii_ in _FRACTIONS.items():
        text = text.replace(glyph, ascii_)
    text = _DECIMAL_COMMA.sub(".", text.replace("⁄", "/"))
    return re.sub(r"\s+", " ", text)


def _value(token: str) -> float:
    parts = token.split()
    if len(parts) == 2:
        return _value(parts[0]) + _value(parts[1])
    if "/" in token:
        num, den = token.split("/", 1)
        return float(num) / float(den) if float(den) else math.nan
    return float(token)


def _tokens(text: str) -> List[str]:
    return _NUMBER.findall(_normalise(text))


def written_values(text: str) -> List[float]:
    """
    Every number ``text`` writes, by value, in order. A mixed number counts whole and then as its
    two parts, because collapsing whitespace can join a whole ("Serves 4") to the fraction that
    began the next line ("½ cup").
    """
    values: List[float] = []
    for token in _tokens(text):
        values.append(_value(token))
        parts = token.split()
        if len(parts) == 2:
            values.extend(_value(p) for p in parts)
    return values


def _word_values(text: str) -> List[float]:
    """
    Every number ``text`` spells as a word, by value. A cardinal before a plural fraction word
    ("two thirds") also yields their product, as well as each part.
    """
    words = _WORD.findall(text.lower())
    values: List[float] = []
    for i, word in enumerate(words):
        if word in _CARDINAL_WORDS:
            values.append(float(_CARDINAL_WORDS[word]))
            following = words[i + 1] if i + 1 < len(words) else ""
            if following in _PLURAL_FRACTION_WORDS:
                values.append(_CARDINAL_WORDS[word] * _FRACTION_WORDS[following])
        elif word in _FRACTION_WORDS:
            values.append(_FRACTION_WORDS[word])
    return values


def unmatched_numbers(source_text: str, lines: Iterable[str]) -> List[str]:
    """
    The numbers ``lines`` write that ``source_text`` never does, as each line writes them (D2).

    A presence check by value, not an alignment: a number the source writes anywhere passes, so a
    converted number that happens to occur elsewhere is missed. It catches rewriting in general;
    the extract prompt's verbatim rule stays the first defence. The source's number words count by
    value ("HALF AN OUNCE" states 0.5); a line's words are not checked.
    """
    source_values = written_values(source_text) + _word_values(source_text)
    source: Set[float] = {round(v, 4) for v in source_values if not math.isnan(v)}
    missing: List[str] = []
    for line in lines:
        for token in _tokens(line):
            if round(_value(token), 4) in source:
                continue
            parts = token.split()
            if len(parts) == 2 and all(round(_value(p), 4) in source for p in parts):
                continue
            missing.append(token)
    return missing
