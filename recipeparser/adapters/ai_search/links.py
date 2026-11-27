"""The links lane: plain links from Brave Search for sites Anthropic's web tools refuse."""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from typing import Literal

from recipeparser.adapters.ai_search.find import recipe_suffix
from recipeparser.adapters.ai_search.look import (
    KNOWN_UNSEARCHABLE,
    MAX_NOTE_CHARS,
    MAX_TITLE_CHARS,
    UNSEARCHABLE,
    RawCandidate,
    _clip,
    _emittable,
    _plain,
    normalise_url,
)
from recipeparser.adapters.brave import BraveResult, links_query

MAX_LINKS = 3


def refused_set() -> frozenset[str]:
    """Every site Anthropic's crawler refuses: the known list plus those learned this process."""
    return KNOWN_UNSEARCHABLE | UNSEARCHABLE.current()


def links_sites(scope: Literal["sites", "web"], cook_hosts: Iterable[str]) -> list[str]:
    """The sites to ask Brave about: the whole refused set for the web (the known sites first, sorted,
    then those learned this process, sorted), or the cook's own sites that are in it (exact host
    membership, as ``cook_sites(skip=...)`` applies the same set)."""
    refused = refused_set()
    if scope == "web":
        return [*sorted(KNOWN_UNSEARCHABLE), *sorted(refused - KNOWN_UNSEARCHABLE)]
    return [host for host in cook_hosts if host in refused]


def look_links_query(question: str, sites: Sequence[str]) -> str | None:
    """The lane's one Brave query: the question plus " recipe" as the find's ``web_query`` adds it,
    restricted to ``sites``. "recipe" is kept whole when a long question is trimmed to Brave's limits
    (F-866: without it Brave offered round-ups and guides). The caller fits ``sites`` first."""
    return links_query(question, sites, suffix=recipe_suffix(question))


def to_links(
    results: Iterable[BraveResult],
    *,
    sites: Sequence[str],
    exclude: Iterable[str],
    offered: Iterable[str],
) -> list[RawCandidate]:
    """Up to ``MAX_LINKS`` of Brave's results, in its order, that may be emitted as plain links."""
    seen: set[str] = set()
    for url in (*exclude, *offered):
        try:
            seen.add(normalise_url(url))
        except ValueError:
            continue
    hosts = set(sites)
    links: list[RawCandidate] = []
    for result in results:
        if len(links) == MAX_LINKS:
            break
        if not _emittable(result.url, hosts):
            continue
        try:
            key = normalise_url(result.url)
        except ValueError:
            continue
        title = _clip(html.unescape(_plain(result.title)), MAX_TITLE_CHARS)
        if key in seen or not title:
            continue
        seen.add(key)
        links.append(
            RawCandidate(
                gap=0,
                url=result.url,
                title=title,
                note=_clip(html.unescape(_plain(result.description)), MAX_NOTE_CHARS),
            )
        )
    return links
