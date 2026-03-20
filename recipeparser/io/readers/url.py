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
    # → [Chunk(text="...", input_type=InputType.URL, source_url="https://...")]
"""

from __future__ import annotations

import logging
from typing import List

import requests  # type: ignore[import-untyped]

from recipeparser.core.models import Chunk, InputType
from recipeparser.io.readers import RecipeReader

log = logging.getLogger(__name__)

_JINA_PREFIX = "https://r.jina.ai/"
_REQUEST_TIMEOUT = 30  # seconds


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
            )
        ]
