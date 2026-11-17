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
    description: str = ""
    profile_name: str = ""
    recipe_title: str = ""
    recipe_description: str = ""
    recipe_publisher: str = ""
    has_recipe: bool = False


def _text(source: object, key: str) -> str:
    """``source[key]`` when ``source`` is a dict and the value is a string; otherwise empty."""
    value = source.get(key) if isinstance(source, dict) else None
    return value if isinstance(value, str) else ""


# Brave's ``q`` accepts at most this many characters and words.
MAX_QUERY_CHARS = 400
MAX_QUERY_WORDS = 50


# The question keeps at least this much of the query (characters; words) when sites are capped.
MIN_QUESTION_CHARS = 150
MIN_QUESTION_WORDS = 10


def _group(sites: Sequence[str]) -> str:
    return "(" + " OR ".join(f"site:{site}" for site in sites) + ")"


def fit_sites(question: str, sites: Sequence[str]) -> list[str]:
    """The longest leading run of ``sites`` whose ``site:`` group leaves the question room in the query.

    The question keeps at least ``MIN_QUESTION_CHARS`` characters (or all of it, when shorter) of
    ``MAX_QUERY_CHARS`` and ``MIN_QUESTION_WORDS`` words of ``MAX_QUERY_WORDS``. Order is the caller's:
    what does not fit is cut from the end. Empty when not even the first site fits."""
    reserve_chars = min(len(question), MIN_QUESTION_CHARS)
    reserve_words = min(len(question.split()), MIN_QUESTION_WORDS)
    kept = list(sites)
    while kept:
        group = _group(kept)
        if len(group) + 1 + reserve_chars <= MAX_QUERY_CHARS and len(group.split()) + reserve_words <= MAX_QUERY_WORDS:
            break
        kept.pop()
    return kept


def links_query(question: str, sites: Sequence[str]) -> str | None:
    """The question restricted to the given sites with ``site:`` operators, within Brave's limits.

    The operator group is kept whole; the question loses whole words from its end until the query
    fits ``MAX_QUERY_CHARS`` and ``MAX_QUERY_WORDS``. None when the operator group alone does not fit.
    """
    group = _group(sites)
    group_words = len(group.split())
    if len(group) > MAX_QUERY_CHARS or group_words > MAX_QUERY_WORDS:
        return None
    words = question.split()
    if len(question) + 1 + len(group) <= MAX_QUERY_CHARS and len(words) + group_words <= MAX_QUERY_WORDS:
        return f"{question} {group}"  # fits as asked
    words = words[: MAX_QUERY_WORDS - group_words]
    while words and len(" ".join(words)) + 1 + len(group) > MAX_QUERY_CHARS:
        words.pop()
    return f"{' '.join(words)} {group}" if words else group


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
                    profile, recipe = item.get("profile"), item.get("recipe")
                    # Only these named fields are read; the recipe block's ingredients and steps are never touched.
                    results.append(
                        BraveResult(
                            url,
                            title,
                            _text(item, "description"),
                            _text(profile, "name"),
                            _text(recipe, "title"),
                            _text(recipe, "description"),
                            _text(recipe, "publisher"),
                            isinstance(recipe, dict),
                        )
                    )
            return results
        except (ValueError, AttributeError, TypeError):
            raise BraveError("Brave returned a malformed body") from None
