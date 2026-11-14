# recipeparser/adapters/ai_search/look.py
"""The look agent behind ``POST /search/look`` (Cayenne AI search Stage 5, §7.3-§7.5).

The agent has two tools, both Anthropic server tools: web search and web fetch, each limited to the
cook's own sites (``allowed_domains``) and to ``max_uses`` calls. It has nothing that reaches the
cook's data. It answers with one JSON object of candidates, and nothing it says is trusted:
``check_candidates`` keeps only an ``https`` address on one of the cook's sites that a web fetch in
this request actually read (§7.4), and ``classify`` decides yours / blocked / importable from the
server's own records (§7.5).

``run_look`` mirrors ``agent.py``'s manual loop: the cancel flag is checked before every model call,
a call is not started with less than ``MIN_CALL_SECONDS`` left, the SDK's retries are off, and each
call's usage is recorded as step ``look``. Server tools run inside one response; a ``pause_turn``
is continued by sending the conversation back, within the same limits. A ninth call is a timeout.

The fetched-address set is read from the response blocks (R0, ruling 15): every top-level
``web_fetch_tool_result`` whose content is a ``web_fetch_result`` adds its ``url``; the error variant
and code execution's output are ignored.

A request whose ``allowed_domains`` name a site Anthropic's crawler refuses fails as a whole with a
400 (ruling 14). ``run_look`` raises ``UnsearchableSites`` with the refused domains; the endpoint
remembers them in ``UNSEARCHABLE`` for a day and retries once without them.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import anthropic

from recipeparser import usage
from recipeparser.adapters.ai_search.agent import (
    BETAS,
    MIN_CALL_SECONDS,
    AgentCancelled,
    AgentFailed,
    AgentTimeout,
)

logger = logging.getLogger(__name__)

MAX_PER_GAP = 3
MAX_CANDIDATES = 8
MAX_TITLE_CHARS = 200
MAX_NOTE_CHARS = 160
MAX_PROGRESS_CHARS = 80
UNSEARCHABLE_TTL_S = 24 * 60 * 60

_REFUSED_DOMAINS = re.compile(r"not accessible to our user agent:\s*\[([^\]]*)\]")
_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class LookLimits:
    searches: int = 4
    fetches: int = 8
    rounds: int = 8
    seconds: float = 60.0
    task_budget: int = 40_000
    max_tokens: int = 16_000


@dataclass(frozen=True)
class RawCandidate:
    """One find: the index of the gap it fills, its address, title and note."""

    gap: int
    url: str
    title: str
    note: str


class UnsearchableSites(Exception):
    """Anthropic refused the request because its crawler may not read these domains."""

    def __init__(self, domains: set[str]) -> None:
        super().__init__(", ".join(sorted(domains)))
        self.domains = domains


class UnsearchableCache:
    """Sites Anthropic's crawler refuses, remembered in this process for ``ttl`` seconds."""

    def __init__(self, ttl: float = UNSEARCHABLE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(self, hosts: Iterable[str]) -> None:
        until = self._clock() + self._ttl
        with self._lock:
            for host in hosts:
                self._until[host] = until

    def current(self) -> frozenset[str]:
        now = self._clock()
        with self._lock:
            for host in [h for h, until in self._until.items() if until <= now]:
                del self._until[host]
            return frozenset(self._until)

    def clear(self) -> None:
        with self._lock:
            self._until.clear()


UNSEARCHABLE = UnsearchableCache()


# -- addresses ------------------------------------------------------------------------------------


def normalise_url(url: str) -> str:
    """``https``, the host lower-cased without ``www.`` (an explicit port kept), the path without a
    trailing slash, no query or fragment. Matches the Cayenne client's normalisation."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").removeprefix("www.")
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return f"https://{netloc}{parts.path.rstrip('/')}"


def url_host(url: str) -> str:
    """The address's host, lower-cased, without ``www.`` and without a port."""
    try:
        return (urlsplit(url.strip()).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def site_of(host: str, sites: Iterable[str]) -> str | None:
    """The cook's site ``host`` belongs to (itself or a subdomain of it), or None."""
    for site in sites:
        if host == site or host.endswith("." + site):
            return site
    return None


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def fetched_urls(response: Any) -> set[str]:
    """The addresses a web fetch in this response read successfully (ruling 15)."""
    urls: set[str] = set()
    for block in _get(response, "content") or []:
        if _get(block, "type") != "web_fetch_tool_result":
            continue
        content = _get(block, "content")
        if _get(content, "type") == "web_fetch_result":
            url = _get(content, "url")
            if isinstance(url, str) and url:
                urls.add(url)
    return urls


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plain(text: str) -> str:
    """``text`` as one line of plain text: tags removed, whitespace collapsed."""
    return _SPACE.sub(" ", _TAG.sub("", text)).strip()


# -- the checks -----------------------------------------------------------------------------------


def check_candidates(
    raw: Iterable[RawCandidate],
    *,
    hosts: set[str],
    fetched: set[str],
    exclude: set[str],
    gap_count: int | None = None,
) -> list[RawCandidate]:
    """The candidates that survive §7.4, cut to the limits: at most 3 per gap and 8 in all.

    A candidate survives when its address is ``https``, on one of ``hosts`` (or a subdomain), read
    by a successful fetch (``fetched``), not in ``exclude`` and not offered already; and, given
    ``gap_count``, its gap is one of those looked for. Addresses compare normalised.
    """
    read = {normalise_url(u) for u in fetched}
    excluded = {normalise_url(u) for u in exclude}
    seen: set[str] = set()
    per_gap: dict[int, int] = {}
    kept: list[RawCandidate] = []
    for c in raw:
        if len(kept) == MAX_CANDIDATES:
            break
        url = c.url.strip()
        if urlsplit(url).scheme.lower() != "https":
            continue
        if site_of(url_host(url), hosts) is None:
            continue
        key = normalise_url(url)
        if key not in read or key in excluded or key in seen:
            continue
        if gap_count is not None and not 0 <= c.gap < gap_count:
            continue
        if per_gap.get(c.gap, 0) == MAX_PER_GAP:
            continue
        title = _clip(_plain(c.title), MAX_TITLE_CHARS)
        if not title:
            continue
        seen.add(key)
        per_gap[c.gap] = per_gap.get(c.gap, 0) + 1
        kept.append(RawCandidate(gap=c.gap, url=url, title=title, note=_clip(_plain(c.note), MAX_NOTE_CHARS)))
    return kept


def classify(
    checked: Iterable[RawCandidate],
    *,
    sites: Mapping[str, str],
    own: Mapping[str, str],
    blocked: set[str],
) -> list[dict[str, Any]]:
    """The wire candidates: ``yours`` (with ``recipe_id``) when the normalised address is one of the
    cook's recipes' (``own``); else ``blocked`` when its host refused an import lately; else
    ``importable``. ``sites`` maps each of the cook's hosts to its display name."""
    out: list[dict[str, Any]] = []
    for c in checked:
        host = url_host(c.url)
        site = site_of(host, sites) or host
        item: dict[str, Any] = {
            "gap": c.gap,
            "url": c.url,
            "title": c.title,
            "site": sites.get(site, site),
            "note": c.note,
        }
        recipe_id = own.get(normalise_url(c.url))
        if recipe_id is not None:
            item["status"] = "yours"
            item["recipe_id"] = recipe_id
        elif host in blocked:
            item["status"] = "blocked"
        else:
            item["status"] = "importable"
        out.append(item)
    return out


# -- the agent ------------------------------------------------------------------------------------


def tool_definitions(hosts: list[str], limits: LookLimits) -> list[dict[str, Any]]:
    return [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": limits.searches,
            "allowed_domains": list(hosts),
        },
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": limits.fetches,
            "allowed_domains": list(hosts),
        },
    ]


def _output_config(limits: LookLimits) -> dict[str, Any]:
    return {"effort": "medium", "task_budget": {"type": "tokens", "total": limits.task_budget}}


def refused_domains(exc: anthropic.APIStatusError) -> set[str]:
    """The domains a 400 says Anthropic's crawler may not read; empty for any other error."""
    texts = [str(getattr(exc, "message", "") or ""), str(exc)]
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            texts.append(str(error.get("message", "")))
    for text in texts:
        match = _REFUSED_DOMAINS.search(text)
        if match:
            return {(a or b).strip().lower().removeprefix("www.") for a, b in _QUOTED.findall(match.group(1))}
    return set()


def _progress(response: Any, on_progress: Callable[[str], None]) -> None:
    for block in _get(response, "content") or []:
        if _get(block, "type") != "server_tool_use":
            continue
        name = _get(block, "name")
        tool_input = _get(block, "input") or {}
        if name == "web_search":
            query = _plain(str(_get(tool_input, "query") or ""))
            if query:
                on_progress(_clip(f"Looking for {query}", MAX_PROGRESS_CHARS))
        elif name == "web_fetch":
            host = url_host(str(_get(tool_input, "url") or ""))
            if host:
                on_progress(_clip(f"Reading a page on {host}", MAX_PROGRESS_CHARS))


def _json_text(text: str) -> Any:
    """The JSON value in ``text``, which may be wrapped in a code fence or a sentence."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    starts = [i for i in (stripped.find("{"), stripped.find("[")) if i >= 0]
    if not starts:
        raise AgentFailed("the final answer carried no JSON")
    start = min(starts)
    end = max(stripped.rfind("}"), stripped.rfind("]"))
    try:
        return json.loads(stripped[start : end + 1])
    except ValueError as exc:
        raise AgentFailed("the final answer was not readable JSON") from exc


def parse_candidates(response: Any) -> list[RawCandidate]:
    """The candidates the final response names, unchecked. Malformed entries are dropped."""
    texts = [str(_get(b, "text") or "") for b in _get(response, "content") or [] if _get(b, "type") == "text"]
    if not texts:
        raise AgentFailed("the final response carried no text block")
    try:
        value = _json_text(texts[-1])
    except AgentFailed:
        value = _json_text("".join(texts))
    items = value.get("candidates") if isinstance(value, Mapping) else value
    if not isinstance(items, list):
        raise AgentFailed("the final answer had no candidate list")
    found: list[RawCandidate] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        gap, url, title, note = item.get("gap"), item.get("url"), item.get("title"), item.get("note", "")
        if isinstance(gap, bool) or not isinstance(gap, int):
            continue
        if not isinstance(url, str) or not isinstance(title, str) or not isinstance(note, str):
            continue
        found.append(RawCandidate(gap=gap, url=url, title=title, note=note))
    return found


def _check(cancel: threading.Event, deadline: float, clock: Callable[[], float]) -> float:
    if cancel.is_set():
        raise AgentCancelled()
    remaining = deadline - clock()
    if remaining <= MIN_CALL_SECONDS:
        raise AgentTimeout()
    return remaining


def run_look(
    *,
    client: anthropic.Anthropic,
    model: str,
    system: list[dict[str, Any]],
    user_text: str,
    hosts: list[str],
    on_progress: Callable[[str], None],
    cancel: threading.Event,
    limits: LookLimits = LookLimits(),
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[RawCandidate], set[str]]:
    """Run the look until it answers; return its unchecked candidates and every address it read.

    Raises ``AgentTimeout``, ``AgentFailed``, ``AgentCancelled`` or ``UnsearchableSites``.
    """
    deadline = clock() + limits.seconds
    tools = tool_definitions(hosts, limits)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
    fetched: set[str] = set()
    calls = 0
    while True:
        remaining = _check(cancel, deadline, clock)
        if calls >= limits.rounds:
            raise AgentTimeout()
        calls += 1
        try:
            response = client.with_options(max_retries=0).beta.messages.create(  # type: ignore[call-overload]
                model=model,
                max_tokens=limits.max_tokens,
                system=system,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config=_output_config(limits),
                betas=BETAS,
                fallbacks="default",
                timeout=remaining,
            )
        except anthropic.APITimeoutError as exc:
            raise AgentTimeout() from exc
        except anthropic.BadRequestError as exc:
            domains = refused_domains(exc)
            if domains:
                raise UnsearchableSites(domains) from exc
            logger.warning("search look: Anthropic refused the request: %s", type(exc).__name__)
            raise AgentFailed(type(exc).__name__) from exc
        except anthropic.APIError as exc:
            logger.warning("search look: Anthropic call failed: %s", type(exc).__name__)
            raise AgentFailed(type(exc).__name__) from exc

        usage.record_claude("look", response.model, response.usage)
        fetched |= fetched_urls(response)
        _progress(response, on_progress)
        stop = response.stop_reason
        if stop == "end_turn":
            return parse_candidates(response), fetched
        if stop != "pause_turn":
            if stop == "refusal":
                logger.warning("search look: refusal %s", getattr(response, "stop_details", None))
            raise AgentFailed(f"stopped with {stop}")
        messages.append({"role": "assistant", "content": response.content})
