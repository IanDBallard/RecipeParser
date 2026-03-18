"""Shared utility functions for file handling and text processing."""
import contextlib
import os
import re
import tempfile
from typing import Generator

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Title-case normalisation
# ---------------------------------------------------------------------------

# Standard culinary/English stop words that stay lowercase when they appear
# in the middle of a title.  The first word of a title is always capitalised
# regardless of this list.
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the",
    "and", "but", "or", "nor", "for", "yet", "so",
    "at", "by", "in", "of", "on", "to", "up", "as",
    "into", "onto", "with", "from", "over", "than",
    "via", "per",
})


def title_case(text: str) -> str:
    """
    Convert *text* to culinary title case.

    Rules:
    - Every word is capitalised except stop words (articles, short prepositions,
      coordinating conjunctions) that appear in the *middle* of the title.
    - The **first** and **last** word are always capitalised.
    - Hyphenated compounds capitalise each part independently
      (e.g. "pan-fried" → "Pan-Fried").
    - Preserves existing all-caps abbreviations (e.g. "BBQ", "NYC").
    - Strips leading/trailing whitespace and collapses internal runs of
      whitespace to a single space.

    Examples::

        title_case("CHOCOLATE CHIP COOKIES")  → "Chocolate Chip Cookies"
        title_case("mac and cheese")           → "Mac and Cheese"
        title_case("the best pan-fried steak") → "The Best Pan-Fried Steak"
        title_case("BBQ ribs with coleslaw")   → "BBQ Ribs with Coleslaw"
    """
    if not text or not text.strip():
        return text

    # Normalise whitespace
    text = re.sub(r"\s+", " ", text.strip())

    # Split on spaces, preserving each token
    words = text.split(" ")
    result: list[str] = []

    for i, word in enumerate(words):
        if not word:
            continue

        is_first = i == 0
        is_last = i == len(words) - 1

        # Handle hyphenated compounds: capitalise every part unconditionally.
        # Stop-word rules do not apply inside a hyphenated compound — each
        # segment is treated as a meaningful word (e.g. "slow-and-low" →
        # "Slow-And-Low", "stir-in" → "Stir-In").
        if "-" in word:
            parts = word.split("-")
            result.append("-".join(_cap_word(part) for part in parts))
            continue

        word_lower = word.lower()

        # Always capitalise first and last word
        if is_first or is_last:
            result.append(_cap_word(word))
        elif word_lower in _STOP_WORDS:
            result.append(word_lower)
        else:
            result.append(_cap_word(word))

    return " ".join(result)


# Explicit allowlist of all-caps tokens that should be preserved as-is.
# A length-based heuristic cannot distinguish "BBQ" from "JOY", so we use
# an allowlist of known culinary, geographic, and common abbreviations.
# Add entries here as needed — all comparisons are case-insensitive.
_PRESERVED_ACRONYMS: frozenset[str] = frozenset({
    # Culinary
    "BBQ", "MSG", "OJ",
    # Geographic
    "NYC", "LA", "SF", "DC", "UK", "US", "EU",
    # Units / measurements
    "TV",
})


def _cap_word(word: str) -> str:
    """
    Capitalise the first letter of *word*, lowercasing the rest.

    Exception: tokens that appear in ``_PRESERVED_ACRONYMS`` are returned
    unchanged regardless of their input casing.

    Examples::

        _cap_word("BBQ")       → "BBQ"      (in allowlist)
        _cap_word("NYC")       → "NYC"      (in allowlist)
        _cap_word("THE")       → "The"      (not in allowlist)
        _cap_word("COOKIES")   → "Cookies"
        _cap_word("CHOCOLATE") → "Chocolate"
        _cap_word("flour")     → "Flour"
    """
    if not word:
        return word
    if word.upper() in _PRESERVED_ACRONYMS:
        return word.upper()  # normalise to canonical all-caps form
    return word[0].upper() + word[1:].lower()


@contextlib.contextmanager
def temp_file_from_upload(upload_file) -> Generator[str, None, None]:
    """
    Context manager that reads an UploadFile (FastAPI), writes it to a
    temporary file on disk, yields the path, and ensures cleanup.
    """
    suffix = os.path.splitext(upload_file.filename or "")[1]
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(upload_file.file.read())
        tmp_path = tmp.name

    try:
        yield tmp_path
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def html_to_text(html_content: str) -> str:
    """
    Convert HTML to plain text using BeautifulSoup, stripping all tags
    and preserving newlines.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text(separator="\n", strip=True)
