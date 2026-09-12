"""
url.py — Reader for web URLs via the r.jina.ai proxy.

Fetches the Markdown-rendered content of a URL using the Jina AI reader
service (``https://r.jina.ai/<url>``), which strips navigation, ads, and
boilerplate and returns clean article text suitable for recipe extraction.

This reader is intentionally thin — all AI work happens downstream in the
pipeline stages.

Usage::

    reader = UrlReader()
    chunks = reader.read("https://www.seriouseats.com/some-recipe")
    # → [Chunk(text="...", input_type=InputType.URL, source_url="https://...",
    #          citation=Citation("web", "seriouseats.com", "seriouseats.com", None))]
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests  # type: ignore[import-untyped]

from recipeparser.core.citation import web_citation
from recipeparser.core.models import Chunk, InputType
from recipeparser.io.readers import RecipeReader

log = logging.getLogger(__name__)

_JINA_PREFIX = "https://r.jina.ai/"
_REQUEST_TIMEOUT = 30  # seconds


@dataclass(frozen=True)
class PageMeta:
    """What a page says about itself in its <head>: the hero image and the description."""

    image_url: Optional[str]
    description: Optional[str]


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_IMAGE_KEYS = ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src")
_DESCRIPTION_KEYS = ("description", "og:description", "twitter:description")


def _meta_tags(html: str) -> Dict[str, str]:
    """property/name → content for every <meta> tag, first occurrence winning."""
    found: Dict[str, str] = {}
    for tag in _META_TAG_RE.findall(html):
        attrs = {}
        for m in _ATTR_RE.finditer(tag):
            attrs[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = html_mod.unescape(attrs.get("content") or "").strip()
        if key and content and key not in found:
            found[key] = content
    return found


def page_meta_from_html(html: str) -> PageMeta:
    """
    The hero image and description a page declares in its own <meta> tags.

    The scraper's markdown carries neither reliably — on 2026-09-12 an NYT page
    came back with no og:image line and, as its only image, the Edamam
    "Powered by" logo, while the page's own head named the real photograph.
    An image that is not an absolute http(s) URL is treated as none.
    """
    tags = _meta_tags(html)
    image = next((tags[k] for k in _IMAGE_KEYS if k in tags), None)
    if image is not None and not image.lower().startswith(("http://", "https://")):
        image = None
    description = next((tags[k] for k in _DESCRIPTION_KEYS if k in tags), None)
    return PageMeta(image_url=image, description=description)


_BADGE_WORDS = frozenset(
    {"logo", "badge", "icon", "sprite", "avatar", "powered", "button", "pixel", "spacer", "placeholder"}
)


def looks_like_badge(url: str, alt: str = "") -> bool:
    """
    True for an image that is a site's furniture rather than a photograph.

    Judged from the URL's path (with a Next.js ``/_next/image?url=…`` wrapper
    unwrapped) and the alt text: a whole badge-word token in either, an SVG,
    or anything the wrapper serves from ``/assets/``. Badge words are matched
    as whole tokens after splitting on non-alphanumerics, so "iconic-lasagna"
    and "silicone-mold-cookies" are not mistaken for site furniture the way a
    plain substring match would. ``parse_qs`` already percent-decodes the
    wrapper's ``url=`` value; unquoting the whole URL before parsing it (as
    opposed to just the path) would let an inner URL's own encoded
    ``?``/``&``/``=`` characters split into the wrong query parameters.
    """
    parsed = urlparse(url)
    inner = parse_qs(parsed.query).get("url", [""])[0].lower()
    path = unquote(parsed.path).lower()
    if path.endswith(".svg") or inner.endswith(".svg"):
        return True
    if inner.startswith("/assets/") or "/assets/" in inner:
        return True
    haystack = " ".join((path, inner, alt.lower()))
    tokens = set(re.split(r"[^a-z0-9]+", haystack))
    return bool(tokens & _BADGE_WORDS)


class UrlReader(RecipeReader):
    """
    Fetches a URL via the r.jina.ai proxy and returns a single Chunk.

    The Jina reader converts the target page to clean Markdown, removing
    navigation, ads, and other non-content elements. The resulting text is
    returned as a single Chunk with ``InputType.URL``.

    Args:
        timeout: HTTP request timeout in seconds (default: 30).
    """

    def __init__(self, timeout: int = _REQUEST_TIMEOUT) -> None:
        self.timeout = timeout

    def read(self, source: str) -> List[Chunk]:
        """
        Fetch ``source`` via r.jina.ai and return a single-element list.

        Args:
            source: The target URL to fetch (e.g. ``https://example.com/recipe``).

        Returns:
            A list containing exactly one Chunk with:
            - ``text``: The Markdown content returned by r.jina.ai.
            - ``input_type``: ``InputType.URL``
            - ``source_url``: The original (non-proxied) URL.
            - ``citation``: a ``web_citation`` of the URL (host-derived key and title)

        Raises:
            requests.HTTPError: If the r.jina.ai request returns a non-2xx status.
            requests.Timeout: If the request exceeds ``self.timeout`` seconds.
            requests.RequestException: For any other network-level failure.
        """
        jina_url = f"{_JINA_PREFIX}{source}"
        log.info("UrlReader: fetching %s via %s", source, jina_url)

        response = requests.get(jina_url, timeout=self.timeout)
        response.raise_for_status()

        text = response.text
        log.info(
            "UrlReader: received %d chars for %s", len(text), source
        )

        return [
            Chunk(
                text=text,
                input_type=InputType.URL,
                source_url=source,
                citation=web_citation(source),
            )
        ]
