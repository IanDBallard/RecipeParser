"""Brave Web Search client for the look's links lane.

One ``httpx.Client`` per instance. Every failure (HTTP error, timeout, non-200,
malformed body) surfaces as ``BraveError``. The subscription key is sent only in
a header; it is never logged and never put in an exception message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import httpx

_BASE_URL = "https://api.search.brave.com"
_SEARCH_PATH = "/res/v1/web/search"


class BraveError(Exception):
    """Brave could not give an answer (network, status or body)."""


@dataclass(frozen=True)
class BraveResult:
    url: str
    title: str


def links_query(question: str, sites: Sequence[str]) -> str:
    """The question restricted to the given sites with ``site:`` operators."""
    scope = " OR ".join(f"site:{site}" for site in sites)
    return f"{question} ({scope})"


class BraveSearch:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    def search(self, query: str, *, count: int = 10) -> list[BraveResult]:
        try:
            response = self._client.get(_SEARCH_PATH, params={"q": query, "count": count})
        except httpx.HTTPError as exc:
            raise BraveError(f"Brave request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise BraveError(f"Brave returned HTTP {response.status_code}")
        try:
            body = response.json()
            raw = (body.get("web") or {}).get("results") or []
            results = []
            for item in raw:
                url, title = item.get("url"), item.get("title")
                if isinstance(url, str) and url and isinstance(title, str) and title:
                    results.append(BraveResult(url, title))
            return results
        except (ValueError, AttributeError, TypeError):
            raise BraveError("Brave returned a malformed body") from None
