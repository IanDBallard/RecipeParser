"""The links lane: plain links from Brave Search for sites Anthropic's web tools refuse."""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from typing import Literal

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
from recipeparser.adapters.brave import BraveResult

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
