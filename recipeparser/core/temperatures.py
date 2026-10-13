"""
recipeparser/core/temperatures.py — find the temperatures a direction states and wrap each in a
temperature token (Cayenne's oven temperatures design, D2-D5), for the backfill.

Deterministic, and only for the forms that leave no doubt: a figure with its unit ("350°F", "180C",
"350 degrees F"), a Gas Mark, and runs of these that give one setting several ways ("180°C/160°C
fan/gas mark 4"). A bare number and a described oven are left for the model (``needs_model``), and
a range is never tagged. No word changes: tokens are spliced around the text that is there.

Pure: no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from recipeparser.core.fat_tokens import TOKEN_RE, locate, parse_temp_value, temp_token

_DEGREE = re.compile(
    r"(?<![\d.])(-?\d{1,3}(?:\.\d)?)\s*(?:°|º|˚|degrees?\s+|deg\.?\s*)\s*(C|F|Celsius|Centigrade|Fahrenheit)\b",
    re.IGNORECASE,
)
_BARE = re.compile(r"(?<![\d.,])(\d{3})\s?(C|F)\b")  # case-sensitive: "180C", "350 F"; never "2 C flour"
_GAS = re.compile(r"\bgas(?:\s+mark)?\s*(¼|½|1/4|1/2|[1-9])(?![\d/])(?!\s*(?:times?|burners?|rings?|hobs?|min(?:ute)?s?|hours?|hrs?|sec(?:ond)?s?)\b)", re.IGNORECASE)
_FAN_AFTER = re.compile(
    r"\s*(?:\(\s*)?(?:fan(?:[- ](?:forced|assisted))?|convection)(?:\s+oven)?(?:\s*\))?", re.IGNORECASE
)
_FAN_WORD = r"(?:fan(?:[- ](?:forced|assisted))?|convection)(?:\s+oven)?"
_FAN_BEFORE = re.compile(_FAN_WORD + r"[\s:]*$", re.IGNORECASE)
_GAP = re.compile(r"^[\s/,()]*(?:or)?[\s/,()]*(?:" + _FAN_WORD + r"[\s/,():]*)?$", re.IGNORECASE)
_CONVENTIONAL_CLOSE = re.compile(r"^(?:\s*(?:conventional|regular|standard)(?:\s+oven)?)?\s*\)", re.IGNORECASE)
_RANGE_BEFORE = re.compile(
    r"(?:\d\s*(?:°\s*[CF]?)?\s*(?:[-–—]|to)|\d\s*/|between\s+\d[\d.]*\s*(?:°\s*[CF]?)?\s+and)\s*$", re.IGNORECASE
)
_RANGE_AFTER = re.compile(r"^\s*(?:[-–—]|to)\s*\d")
_OVEN_WORDS = re.compile(r"\b(?:oven|bake[sd]?|baking|roast(?:s|ed|ing)?|pre-?heat(?:s|ed|ing)?)\b", re.IGNORECASE)
_PROBE_WORDS = re.compile(
    r"\b(?:thermometer|internal|reads?|registers?|reach(?:es|ed)?|shows?|temp(?:erature)?|core|cent(?:re|er)|probe|inside"
    r"|oil|fry|frying|deep-fry|fat|syrup|sugar|caramel|candy|water|boil)\b",
    re.IGNORECASE,
)
_CLAUSE_START = ".,;!?("
_CLAUSE_END = ".,;!?)"
_DESCRIBED_OVEN = re.compile(r"\b(?:(?:very|moderately)\s+)?(?:slow|cool|moderate|hot|quick)\s+oven\b", re.IGNORECASE)
_BARE_OVEN_NUMBER = re.compile(
    r"(?<![\d.,/-])([1-4]\d\d|500)(?!\d|[.,]\d|/|\s*(?:°|[-–—]|x|×|%|g\b|kg|ml|cm|mm|in\b|inch|min|hour|hr|sec|C\b|F\b))",
    re.IGNORECASE,
)
_MARKS = {"¼": 0.25, "1/4": 0.25, "½": 0.5, "1/2": 0.5}


@dataclass
class Found:
    start: int
    end: int
    value: str
    words: str


@dataclass
class _Reading:
    start: int
    end: int
    scale: str  # "C" | "F" | "gas"
    number: float
    fan: bool = False


def _number(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def _token_spans(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def _overlaps(start: int, end: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(start < e and s < end for s, e in spans)


def _sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(c, 0, start) for c in ".!?;")
    right = min((i for i in (text.find(c, end) for c in ".!?;") if i >= 0), default=len(text))
    return text[left + 1:right]


def _readings(text: str) -> List[_Reading]:
    spans = _token_spans(text)
    found: List[_Reading] = []
    for m in _DEGREE.finditer(text):
        scale = "F" if m.group(2)[0].upper() == "F" else "C"
        found.append(_Reading(m.start(), m.end(), scale, float(m.group(1))))
    for m in _BARE.finditer(text):
        found.append(_Reading(m.start(), m.end(), m.group(2), float(m.group(1))))
    for m in _GAS.finditer(text):
        mark = _MARKS.get(m.group(1)) or float(m.group(1))
        found.append(_Reading(m.start(), m.end(), "gas", mark))
    found.sort(key=lambda r: (r.start, -r.end))
    kept: List[_Reading] = []
    for r in found:
        if kept and r.start < kept[-1].end:
            continue  # "350°F" matched by both patterns: keep the first, longest
        if _overlaps(r.start, r.end, spans):
            continue  # inside a token already there
        if _RANGE_BEFORE.search(text[:r.start]) or _RANGE_AFTER.match(text[r.end:]):
            continue  # a range is never tagged
        if r.scale != "gas":
            fan = _FAN_AFTER.match(text, r.end)
            if fan and fan.end() > r.end and not re.match(r"\s*\d", text[fan.end():]):
                r.end, r.fan = fan.end(), True  # "180°C fan"; not "180°C (fan 160°C)", where fan labels the next
            elif _FAN_BEFORE.search(text[:r.start]):
                r.fan = True  # "(fan 160°C)": the word before labels this figure
        kept.append(r)
    return kept


def _runs(text: str, readings: List[_Reading]) -> List[List[_Reading]]:
    runs: List[List[_Reading]] = []
    for r in readings:
        if runs and _GAP.match(text[runs[-1][-1].end:r.start]):
            runs[-1].append(r)
        else:
            runs.append([r])
    return runs


def _value(text: str, run: List[_Reading]) -> Optional[str]:
    degrees = [r for r in run if r.scale != "gas" and not r.fan]
    gas = [r for r in run if r.scale == "gas"]
    fans = [r for r in run if r.fan]
    main = degrees[0] if degrees else (gas[0] if gas else None)
    fan = fans[0] if fans else None
    before = text[:run[0].start]
    clause = before[max(before.rfind(c) for c in _CLAUSE_START) + 1:]
    tail = text[run[-1].end:]
    clause += tail[:min((i for i in (tail.find(c) for c in _CLAUSE_END) if i >= 0), default=len(tail))]
    celsius = None if main is None or main.scale == "gas" else (
        main.number if main.scale == "C" else (main.number - 32) * 5 / 9)
    oven = bool(gas or fan) or (
        _OVEN_WORDS.search(clause) is not None and _PROBE_WORDS.search(clause) is None
        and celsius is not None and celsius >= 100
    )
    parts: List[str] = []
    if main is not None:
        parts.append(f"gas{_number(main.number)}" if main.scale == "gas" else f"{_number(main.number)}{main.scale}")
    if oven and fan is None:
        parts.append("oven")
    if fan is not None:
        if main is not None:
            parts.append("oven")
        parts.append(f"fan={_number(fan.number)}{fan.scale}")
    value = ",".join(parts)
    return value if parse_temp_value(value) is not None else None


def find_temperatures(text: str) -> List[Found]:
    """Every clear temperature in ``text``, as runs; positions are ``text``'s. Never inside a token."""
    out: List[Found] = []
    for run in _runs(text, _readings(text)):
        start, end = run[0].start, run[-1].end
        words = text[start:end]
        if words.count("(") > words.count(")") and (close := _CONVENTIONAL_CLOSE.match(text[end:])):
            end += close.end()
            words = text[start:end]
        plain = [r for r in run if not r.fan]
        if len({r.scale for r in plain}) < len(plain):
            continue  # two figures of one scale: a range or an alternative, never tagged
        value = _value(text, run)
        if value is not None:
            out.append(Found(start, end, value, words))
    return out


def splice(text: str, found: Sequence[Found]) -> str:
    pieces: List[str] = []
    cursor = 0
    for f in sorted(found, key=lambda f: f.start):
        pieces.append(text[cursor:f.start])
        pieces.append(temp_token(f.value, f.words))
        cursor = f.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def needs_model(text: str) -> bool:
    """Whether a step, after the splice, may still state a temperature only a model can read: a
    described oven, or a bare figure in a sentence about the oven."""
    plain = TOKEN_RE.sub(lambda m: " " if m.group(1).startswith("temp:") else m.group(2), text)
    if _DESCRIBED_OVEN.search(plain):
        return True
    for m in _BARE_OVEN_NUMBER.finditer(plain):
        if _OVEN_WORDS.search(_sentence(plain, m.start(), m.end())):
            return True
    return False


def place_temperature_mentions(text: str, mentions: Sequence[Tuple[str, str, str]]) -> Tuple[str, List[str]]:
    """Splice the model's ``(quote, context, value)`` mentions into ``text`` where each quote is found
    verbatim, outside every token. A bad value, a quote not found or found twice without a context to
    tell them apart, or one overlapping a token or another placement, is dropped and reported."""
    dropped: List[str] = []
    placed: List[Found] = []
    for quote, context, value in mentions:
        if parse_temp_value(value) is None or not quote or any(c in quote for c in "{}|"):
            dropped.append(f"{quote!r}: unusable value {value!r} or quote")
            continue
        spans = _token_spans(text) + [(p.start, p.end) for p in placed]
        starts = [s for s in locate(text, quote, context) if not _overlaps(s, s + len(quote), spans)]
        if len(starts) != 1:
            dropped.append(f"{quote!r} ({context!r}): {len(starts)} free placement(s)")
            continue
        placed.append(Found(starts[0], starts[0] + len(quote), value, text[starts[0]:starts[0] + len(quote)]))
    return splice(text, placed), dropped
