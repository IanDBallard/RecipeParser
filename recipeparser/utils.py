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

    Every recipe title is stored in this form (the title-case titles ruling,
    2026-10-03), so the rules must hold for any title, not only ALL-CAPS ones.

    Rules:
    - Every word is capitalised except stop words (articles, short prepositions,
      coordinating conjunctions) that appear in the *middle* of the title.
    - The **first** and **last** word, and the word after a colon, are always
      capitalised.
    - A word's first *letter* is capitalised, past any leading punctuation
      ("(VEGAN)" → "(Vegan)").
    - Hyphenated compounds capitalise each part independently
      (e.g. "pan-fried" → "Pan-Fried").
    - Preserves the allowlisted abbreviations (e.g. "BBQ", "NYC") in any title.
    - In a title that has lowercase letters, an all-caps word of up to three
      letters standing on its own is an acronym and is kept ("BLT", "XO"); one
      inside a run of capitals is part of a shouted name ("CHOLAR DAL"). An
      ALL-CAPS title carries no such evidence, so only the allowlist survives.
    - A word shaped like "McDonald's", "MacArthur" or "eBay" keeps its shape.
    - Foreign particles ("con", "e", "von", "à la") stay lowercase mid-title,
      unless the writer capitalised one in a mixed-case title ("Ma La").
    - Inner stop words of a hyphenated compound stay lowercase
      ("Sweet-and-Sour").
    - Strips leading/trailing whitespace and collapses internal runs of
      whitespace to a single space.

    Examples::

        title_case("CHOCOLATE CHIP COOKIES")  → "Chocolate Chip Cookies"
        title_case("mac and cheese")           → "Mac and Cheese"
        title_case("the best pan-fried steak") → "The Best Pan-Fried Steak"
        title_case("BBQ ribs with coleslaw")   → "BBQ Ribs with Coleslaw"
        title_case("BLT sandwich")             → "BLT Sandwich"
    """
    if not text or not text.strip():
        return text

    # Normalise whitespace
    text = re.sub(r"\s+", " ", text.strip())
    has_lowercase = any(c.islower() for c in text)

    # Split on spaces, preserving each token
    words = text.split(" ")
    caps = [_is_caps(_core(w)[1]) for w in words]
    result: list[str] = []

    for i, word in enumerate(words):
        # A clause starts the title, follows a colon, or follows a dash standing on its own
        # ("Blondies - the best"); the word after a dash glued to it is handled below.
        starts_clause = i == 0 or words[i - 1].endswith(":") or words[i - 1] in _DASHES
        is_last = i == len(words) - 1
        # A short capitals word is an acronym only on its own: inside a run of capitals
        # ("CHOLAR DAL Creamy Dal") it is part of a shouted name.
        acronym_ok = has_lowercase and not (i > 0 and caps[i - 1]) and not (not is_last and caps[i + 1])

        # An em or en dash glued between words ("chicken—my version") opens a phrase.
        segments = re.split(r"([\u2013\u2014])", word)
        out = []
        for k, segment in enumerate(segments):
            if segment in ("\u2013", "\u2014") or segment == "":
                out.append(segment)
                continue
            out.append(_case_word(
                segment,
                starts_clause=starts_clause if k == 0 else True,
                is_last=is_last and k == len(segments) - 1,
                acronym_ok=acronym_ok,
                has_lowercase=has_lowercase,
            ))
        result.append("".join(out))

    return " ".join(result)


# A dash standing alone between words separates a subtitle: the word after it starts a phrase.
_DASHES: frozenset[str] = frozenset({"-", "\u2013", "\u2014"})

# A French or Italian elided article or preposition glued to its noun: "l'Alsacienne",
# "d'Agneau", "dell'Orto". The article stays lowercase mid-title and the noun is capitalised.
_ELISION_RE = re.compile(r"^(l|d|qu|dell|all|nell|sull|dall)(['\u2019])(.+)$", re.IGNORECASE)


def _case_word(word: str, *, starts_clause: bool, is_last: bool, acronym_ok: bool, has_lowercase: bool) -> str:
    """Case one space-separated word of a title (see title_case for the rules)."""
    lead, core, trail = _core(word)
    elision = _ELISION_RE.match(core)
    if elision:
        article, apostrophe, noun = elision.groups()
        article = article.capitalize() if starts_clause else article.lower()
        return lead + article + apostrophe + _cap_word(noun, acronym_ok) + trail

    # Hyphenated compounds: the first and last parts are capitalised, an inner stop word or
    # particle stays lowercase ("sweet-and-sour" → "Sweet-and-Sour", "gajar-ka-halva" →
    # "Gajar-ka-Halva", "stir-in" → "Stir-In").
    if "-" in word and word not in _DASHES:
        parts = word.split("-")
        return "-".join(
            part.lower() if 0 < j < len(parts) - 1 and _core(part)[1].lower() in _STOP_WORDS | _PARTICLES
            else _cap_word(part, acronym_ok)
            for j, part in enumerate(parts)
        )

    # The writer's capital on the first word inside brackets stands in a mixed-case title
    # ("(The World's Best Cake)"); a lowercase one stays lowercase ("(or vadees)").
    opens_bracket = lead.endswith("(")
    if not (starts_clause or is_last) and core.lower() in _STOP_WORDS:
        if opens_bracket and has_lowercase and core[:1].isupper() and core[1:] == core[1:].lower():
            return word
        return word.lower()
    if not (starts_clause or is_last) and core.lower() in _PARTICLES:
        # The writer's own capital on a particle in a mixed-case title stands ("Ma La").
        kept = has_lowercase and core[:1].isupper() and core[1:] == core[1:].lower()
        return word if kept else word.lower()
    return _cap_word(word, acronym_ok)


# Explicit allowlist of all-caps tokens that should be preserved as-is.
# A length-based heuristic cannot distinguish "BBQ" from "JOY", so we use
# an allowlist of known culinary, geographic, and common abbreviations.
# Add entries here as needed — all comparisons are case-insensitive.
_PRESERVED_ACRONYMS: frozenset[str] = frozenset({
    # Culinary
    "BBQ", "MSG", "OJ",
    # Dietary ("GF and DF": gluten-free, dairy-free)
    "GF", "DF",
    # Geographic ("LA" is not here: "à la" is far commoner in a recipe title)
    "NYC", "SF", "DC", "UK", "US", "EU",
    # Units / measurements
    "TV",
})

# Foreign articles and prepositions that stay lowercase mid-title ("Chilli con Carne",
# "Aglio e Olio", "Nusstorte von Hammerstein"). Unlike an English stop word, one the writer
# capitalised in a mixed-case title is kept: "Ma La" is a name, not "with the".
_PARTICLES: frozenset[str] = frozenset({
    "à", "al", "alla", "au", "aux", "con", "da", "de", "del", "della", "der", "des", "di",
    "du", "e", "el", "en", "et", "la", "le", "mit", "ohne", "und", "van", "von", "y",
    # Hindi and Urdu "of" and "and" ("Khare Masale ka Gosht", "Kala Chana aur Aloo")
    "ka", "ke", "ki", "aur",
})

# Words whose inner capital is deliberate: "McDonald's", "MacArthur", "eBay", "iPhone", "BraveTart".
_KEEP_SHAPE_RE = re.compile(r"^(?:Ma?c[A-Z][a-z']+|[a-z][A-Z][a-z']+|[A-Z][a-z]{2,}(?:[A-Z][a-z']{2,})+)$")


def _core(word: str) -> tuple[str, str, str]:
    """Split *word* into its leading punctuation, its letters and digits, and its trailing punctuation."""
    # Letters are any script's ("éclairs" begins with a letter, not punctuation).
    match = re.fullmatch(r"([\W_]*)(.*?)([\W_]*)", word)
    assert match is not None  # every string matches
    lead, core, trail = match.groups()
    return lead, core, trail


def _is_caps(core: str) -> bool:
    """A word of two or more letters written wholly in capitals."""
    return len(core) >= 2 and core.isupper()


def _cap_word(word: str, acronym_ok: bool = False) -> str:
    """
    Capitalise the first letter of *word*, lowercasing the rest. Leading and
    trailing punctuation is kept as it is.

    Exceptions: tokens in ``_PRESERVED_ACRONYMS`` are returned in capitals
    regardless of their input casing; when *acronym_ok* (a mixed-case title,
    and the word is not inside a run of capitals), a word of up to three
    capitals that is not a stop word or particle, and a ``_KEEP_SHAPE_RE``
    word, are returned unchanged.

    Examples::

        _cap_word("BBQ")       → "BBQ"      (in allowlist)
        _cap_word("THE")       → "The"      (not in allowlist)
        _cap_word("(VEGAN)")   → "(Vegan)"
        _cap_word("BLT", True) → "BLT"      (acronym in a mixed-case title)
        _cap_word("flour")     → "Flour"
    """
    if not word:
        return word
    lead, core, trail = _core(word)
    if core.upper() in _PRESERVED_ACRONYMS:
        core = core.upper()  # normalise to canonical all-caps form
    elif _KEEP_SHAPE_RE.match(core) or (
        acronym_ok
        and _is_caps(core)
        and len(core) <= 3
        and core.lower() not in _STOP_WORDS | _PARTICLES
    ):
        pass
    else:
        core = core[:1].upper() + core[1:].lower()
    return lead + core + trail


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
