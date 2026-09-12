"""
recipeparser/core/citation.py — where a recipe came from, as four columns.

Provenance is written as structured citation fields at insert time, derived
deterministically wherever a deterministic source exists, and the free-text
`source` stays as the display string (design 2026-09-11). This module is the
one place the derivation lives: readers call the constructors, `assemble()`
calls `resolve_citation`, and the backfill script calls `classify_source`.

Pure: no I/O, no imports from recipeparser.io or recipeparser.adapters.
`normalise_key` is mirrored in the Cayenne client
(`cayenne-web/src/lib/domain/citation.ts`); tests/fixtures/citation_keys.json
is the contract both must satisfy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple, Optional
from urllib.parse import urlparse

KINDS = ("book", "web", "periodical", "person", "unknown")
UNKNOWN_BOOK_KEY = "unknown-book"
AUTO_IMPORT_STRINGS = ("EPUB Auto-Import", "PDF Auto-Import")

_BOOK_SEPARATOR = " \u2014 "  # " — ", what the book readers wrote between title and author
_SCHEME_AND_WWW = re.compile(r"^(https?://)?(www\.)?", re.I)
_HTTP = re.compile(r"^https?://", re.I)
_QUOTES = re.compile("[\u2018\u2019\u201c\u201d'\"`]")
_SEPARATORS = re.compile("[\\s\\-\u2013\u2014_.,:;/]+")
_DOMAIN = re.compile(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)
_DOMAIN_THEN_TEXT = re.compile(r"^(?P<host>(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+)\s+(?P<rest>\S.*)$", re.I)


@dataclass(frozen=True)
class Citation:
    kind: str
    key: Optional[str]
    title: Optional[str]
    author: Optional[str]

    def display(self) -> Optional[str]:
        """The free-text `source` for a row that has nothing better: 'Title — Author', or the title."""
        if self.title and self.author:
            return f"{self.title}{_BOOK_SEPARATOR}{self.author}"
        return self.title


def _clean(value: Optional[str]) -> Optional[str]:
    stripped = (value or "").strip()
    return stripped or None


def normalise_key(value: str) -> str:
    """The key two source strings share when a cook would call them one source. Never shown."""
    v = value.strip().lower()
    v = _SCHEME_AND_WWW.sub("", v, count=1)
    v = _QUOTES.sub("", v)
    return _SEPARATORS.sub(" ", v).strip()


def host_of(url_or_host: str) -> str:
    """The host, lower-cased, without a leading www.; a bare domain passes through."""
    v = url_or_host.strip()
    host = urlparse(v).netloc if _HTTP.match(v) else v
    return host.lower().removeprefix("www.")


def book_citation(title: Optional[str], author: Optional[str]) -> Citation:
    """A book the reader knows by its metadata; without a title it is an unknown book."""
    clean_title = _clean(title)
    if clean_title is None:
        return Citation("unknown", UNKNOWN_BOOK_KEY, None, None)
    return Citation("book", normalise_key(clean_title), clean_title, _clean(author))


def web_citation(url_or_host: str, site_name: Optional[str] = None, byline: Optional[str] = None) -> Citation:
    """A page: the key is the host; the title is the site's stated name, else the host."""
    host = host_of(url_or_host)
    return Citation("web", host, _clean(site_name) or host, _clean(byline))


class Classified(NamedTuple):
    rule: int
    citation: Citation


def classify_source(text: Optional[str], byline: Optional[str] = None) -> Classified:
    """
    The design's ordered rules over a free-text source string; first match wins.
    The rule number is returned so the backfill can report its counts per rule.
    """
    value = _clean(text)
    author = _clean(byline)
    if value is None:
        return Classified(1, Citation("unknown", None, None, author))
    if _BOOK_SEPARATOR in value:
        title, book_author = (part.strip() for part in value.split(_BOOK_SEPARATOR, 1))
        return Classified(2, book_citation(title, book_author or author))
    if value in AUTO_IMPORT_STRINGS:
        return Classified(3, Citation("unknown", UNKNOWN_BOOK_KEY, None, None))
    if _DOMAIN.match(value) or _HTTP.match(value):
        return Classified(4, web_citation(value, byline=author))
    domain_then_text = _DOMAIN_THEN_TEXT.match(value)
    if domain_then_text:
        return Classified(5, web_citation(domain_then_text.group("host"), byline=domain_then_text.group("rest")))
    if value.lower() == "gemini":
        # The model named itself. A prompt defect, fixed alongside; the value is not a source.
        return Classified(6, Citation("unknown", None, None, None))
    return Classified(7, Citation("person", normalise_key(value), value, author))


def resolve_citation(known: Optional[Citation], stated_source: Optional[str], byline: Optional[str]) -> Citation:
    """
    The reader's knowledge beats the model's reading; the model fills only what the reader could not.

    A book with metadata, and an unknown book, are settled by the reader. A page keeps its host as
    the key and takes the site's stated name and the byline from the model. With nothing known, the
    model's stated source is classified by the same rules the backfill uses.
    """
    if known is None:
        return classify_source(stated_source, byline).citation
    if known.kind == "web":
        return Citation("web", known.key, _clean(stated_source) or known.title, known.author or _clean(byline))
    return known
