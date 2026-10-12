"""
recipeparser/core/headings.py — the text rules for an ingredient-list heading
(Cayenne ingredient headings design, docs/superpowers/specs/2026-10-12-ingredient-headings-design.md).

REFINE decides which entries are headings (``kind``); these rules only clean a heading's display
name (D3) and, for the one-off backfill of rows imported before ``kind`` existed, say which lines
look like headings. ``heading_shape`` is deliberately conservative: a line it calls "unsure" is
left to the owner's review, never guessed.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import re
from typing import Literal, Optional

_EMPHASIS = "*_ \t"
_BOLD = re.compile(r"^\*\*.+\*\*$")
_FOR = re.compile(r"(?i)^for (the|a|an) \S")
_SET_PHRASES = {"to serve", "to finish", "to garnish", "to grill", "garnish", "optional", "for serving"}


def heading_name(text: str) -> str:
    """The display text of a heading line: emphasis markers and a trailing colon removed, case kept."""
    name = text.strip().strip(_EMPHASIS).rstrip(":").strip(_EMPHASIS)
    return name or text.strip()


def heading_shape(text: str) -> Optional[Literal["heading", "unsure"]]:
    """
    "heading" when the line reads as a section heading by rule, "unsure" for a short title-case
    line that could be a heading or an unquantified food ("Lemon Sauce" beside "Sugar"), None
    otherwise. A line with a digit is never a heading by rule: quantities carry digits.
    """
    line = text.strip()
    if not line or any(ch.isdigit() for ch in line):
        return None
    name = heading_name(line)
    letters = [ch for ch in name if ch.isalpha()]
    if not letters:
        return None
    if line.rstrip(_EMPHASIS).endswith(":") or _BOLD.match(line):
        return "heading"
    if len(letters) >= 3 and all(ch.isupper() for ch in letters):
        return "heading"
    if _FOR.match(name) or name.lower() in _SET_PHRASES:
        return "heading"
    words = name.split()
    if len(words) <= 4 and words[0][0].isupper() and all(w[0].isupper() for w in words if w[0].isalpha()):
        return "unsure"
    return None
