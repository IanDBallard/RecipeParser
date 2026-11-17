"""The find rules: the Brave queries for Add Recipe's search turn, and what becomes a row (pure)."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence

from recipeparser.adapters.ai_search.look import (
    MAX_NOTE_CHARS,
    MAX_TITLE_CHARS,
    RawCandidate,
    _clip,
    _emittable,
    _plain,
    normalise_url,
    site_of,
    url_host,
)
from recipeparser.adapters.brave import BraveResult, fit_sites, links_query

SITES_MAX = 3
WEB_MAX = 5
VIDEO_SOCIAL_HOSTS: frozenset[str] = frozenset(
    {"youtube.com", "tiktok.com", "instagram.com", "pinterest.com", "facebook.com", "x.com", "reddit.com"}
)

_ENDS_WITH_RECIPE = re.compile(r"recipes?$", re.IGNORECASE)
_SEPARATORS = (" | ", " - ", " \u2013 ")
_AMP = re.compile(r"^([^?#]*?)/amp/?([?#].*)?$", re.DOTALL)


def web_query(query: str) -> str:
    """The question plus " recipe", unless it already ends in "recipe" or "recipes"."""
    return query if _ENDS_WITH_RECIPE.search(query.strip()) else f"{query} recipe"


def sites_query(query: str, hosts: Sequence[str]) -> str | None:
    """The web query restricted to the hosts that fit; None when none fit or there are none."""
    question = web_query(query)
    fitted = fit_sites(question, hosts)
    return links_query(question, fitted) if fitted else None


def _clean(text: str) -> str:
    return html.unescape(_plain(text))


def row_title(result: BraveResult) -> str:
    """The recipe block's title, else the page title less a suffix that names the site."""
    if recipe_title := _clean(result.recipe_title):
        return _clip(recipe_title, MAX_TITLE_CHARS)
    title = _clean(result.title)
    cut = max(((title.rfind(sep), sep) for sep in _SEPARATORS), key=lambda found: found[0])
    index, sep = cut
    if index > 0:
        head, suffix = title[:index].strip(), title[index + len(sep) :].strip().lower()
        host = url_host(result.url)
        names = [_clean(result.profile_name).lower(), _clean(result.recipe_publisher).lower()]
        if head and suffix and (suffix in (host, f"www.{host}") or any(suffix in name for name in names)):
            title = head
    return _clip(title, MAX_TITLE_CHARS)


def row_note(result: BraveResult) -> str:
    """The recipe block's description, else the result's, plain and clipped."""
    return _clip(_clean(result.recipe_description) or _clean(result.description), MAX_NOTE_CHARS)


def site_names(results: Iterable[BraveResult]) -> dict[str, str]:
    """Host -> Brave's name for the site, cleaned like a title; the first non-blank name on each host wins."""
    names: dict[str, str] = {}
    for result in results:
        if name := _clean(result.profile_name).strip():
            names.setdefault(url_host(result.url), name)
    return names


def strip_amp(url: str) -> str:
    """The address without one trailing ``/amp`` or ``/amp/`` path segment; query and fragment stay."""
    match = _AMP.match(url)
    if match is None or "://" not in match.group(1):
        return url
    return match.group(1) + (match.group(2) or "")


def _on_video_social(url: str) -> bool:
    return site_of(url_host(url), VIDEO_SOCIAL_HOSTS) is not None


def pick(
    results: Iterable[BraveResult],
    *,
    hosts: set[str] | None,
    seen: set[str],
    limit: int,
    recipes_first: bool = False,
) -> list[RawCandidate]:
    """Up to ``limit`` rows from Brave's results, in its order (recipe-block results first when asked).

    ``seen`` holds the normalised addresses already taken; each kept address is added to it."""
    stripped = [(strip_amp(r.url), r) for r in results]
    if recipes_first:
        stripped.sort(key=lambda pair: not pair[1].has_recipe)
    rows: list[RawCandidate] = []
    for url, result in stripped:
        if len(rows) == limit:
            break
        if not _emittable(url, hosts) or _on_video_social(url):
            continue
        try:
            key = normalise_url(url)
        except ValueError:
            continue
        title = row_title(result)
        if key in seen or not title:
            continue
        seen.add(key)
        rows.append(RawCandidate(gap=0, url=url, title=title, note=row_note(result)))
    return rows
