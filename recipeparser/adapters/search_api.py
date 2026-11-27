"""POST /search/ask: the agent behind a deeper Library search, streamed (Cayenne AI search §7.1).
POST /search/look: the look on the cook's own sites (``scope`` "sites", the default) or on the open
web (``scope`` "web") for an answer's gaps (Stage 5, §7.2).
POST /search/find: Add Recipe's search turn, Brave Search alone on the cook's sites and on the web at
once, no model; one JSON reply, capped by the gate's find windows (Brave search turn, 2026-11-17).

Refusals before the agent starts are ordinary JSON errors: 401 (token), 422 (body), 503 (not
configured, or paused) and 429 (the search rate limit, with Retry-After). The gate's slot is spent
immediately before the stream starts. After that the response is NDJSON (plan D5): progress lines,
then exactly one ``answer`` line (a look: one ``candidates`` line) or one ``error`` line, never an
HTML 500. A look spends one search slot, as an ask does.

The agent runs in a worker thread inside ``usage.attributed(sub, "search")``; its progress phrases
cross to the response through the event loop's thread-safe hand-off. When the cook leaves, the
stream's ``finally`` sets the cancel event and the agent stops before its next model call.

This module never imports ``api.py``: every collaborator arrives through ``build_router``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from recipeparser import usage
from recipeparser.adapters import refusing_hosts
from recipeparser.adapters.ai_gate import AiGate
from recipeparser.adapters.ai_search.agent import AgentCancelled, AgentTimeout, run_agent
from recipeparser.adapters.ai_search.ai_db import AiReaderDb, cook_categories, cook_sites, own_source_urls
from recipeparser.adapters.ai_search.answer import (
    MAX_ALTERNATIVES,
    MAX_HISTORY,
    Answer,
    NoneAnswer,
    Previous,
    RecipesAnswer,
    Turn,
    finalize,
    raw_ids,
)
from recipeparser.adapters.ai_search.find import SITES_MAX, WEB_MAX, pick, site_names, sites_query, web_query
from recipeparser.adapters.ai_search.links import links_sites, look_links_query, to_links
from recipeparser.adapters.ai_search.look import (
    KNOWN_UNSEARCHABLE,
    MAX_URL_CHARS,
    UNSEARCHABLE,
    WEB_BLOCK_REFUSED,
    LookLimits,
    RawCandidate,
    UnsearchableSites,
    check_candidates,
    classify,
    normalise_url,
    run_look,
    url_host,
)
from recipeparser.adapters.ai_search.look_prompt import (
    look_request_text,
    look_system_blocks,
    web_look_request_text,
    web_look_system_blocks,
)
from recipeparser.adapters.ai_search.prompt import request_text, system_blocks
from recipeparser.adapters.ai_search.recipe_pages import PageFetcher, keep_recipe_pages
from recipeparser.adapters.ai_search.tools import ToolBox
from recipeparser.adapters.brave import BraveError, BraveResult, BraveSearch, fit_sites
from recipeparser.io.readers.url import fetch_page_html

logger = logging.getLogger(__name__)

NOT_CONFIGURED_DETAIL = "Searching more deeply isn't available right now."
TIMEOUT_DETAIL = "That took too long to answer. Try again."
FAILED_DETAIL = "Couldn't answer that just now. Try again."
MEDIA_TYPE = "application/x-ndjson"
WORKER_NAME = "search-ask"
LOOK_WORKER_NAME = "search-look"
NO_SITES_DETAIL = "None of your recipes come from a website."
NO_SEARCHABLE_SITES_DETAIL = "None of the sites you use can be searched."
MAX_EXCLUDE = 100
# Requests a look may make while Anthropic keeps refusing sites; each refusal is an unbilled 400.
MAX_SITE_ATTEMPTS = 4
# The links lane's one Brave request: how long the look waits for it, measured from its submission,
# and the model name its ai_usage row carries.
LINKS_TIMEOUT_S = 5.0
LINKS_MODEL = "brave-web-search"
LINKS_STEP = "look_links"
LINKS_WORKER_NAME = "search-look-links"
# The find's Brave requests: the usage step they are recorded under (the model is LINKS_MODEL), how
# long the find waits for them from submission, and the reply's own refusals.
FIND_STEP = "find"
FIND_TIMEOUT_S = 5.0
FIND_WORKER_NAME = "search-find"
MAX_QUERY_CHARS = 120
FIND_UNSET_DETAIL = "Searching the web isn't set up."
FIND_FAILED_DETAIL = "Couldn't search the web just now."

_ANSWER: TypeAdapter[Answer] = TypeAdapter(Answer)


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    today: date  # the device's local date
    measuring: str = Field(min_length=1, max_length=60)  # D1, e.g. "metric, °C"
    filters: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=12)  # D1
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY)  # every earlier turn, oldest first
    previous: Previous | None = None  # the previous release's one turn; accepted for one release

    @model_validator(mode="after")
    def _one_way_to_send_the_conversation(self) -> AskRequest:
        if self.previous is None:
            return self
        if self.history:
            raise ValueError("send history or previous, not both")
        self.history = [Turn(question=self.previous.question, answer=self.previous.answer, chosen=0)]
        self.previous = None
        return self


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _error(code: str, detail: str) -> dict[str, Any]:
    return {"type": "error", "code": code, "detail": detail}


def _work(
    *,
    body: AskRequest,
    claims: dict[str, Any],
    db: AiReaderDb,
    client: anthropic.Anthropic,
    get_gemini: Callable[[], Any],
    get_service_client: Callable[[], Any],
    model: str,
    cancel: threading.Event,
    emit: Callable[[dict[str, Any] | None], None],
) -> None:
    """Run the agent and emit its lines; always ends with ``emit(None)``."""
    # Imported here so a test can replace gemini.get_embeddings, as /embed's tests do.
    from recipeparser import gemini  # noqa: PLC0415

    sub = str(claims.get("sub", ""))
    started = time.monotonic()
    kind = "failed"
    rounds = tokens = 0
    try:
        with usage.attributed(sub, "search", get_client=get_service_client) as ctx:
            try:
                catalogue = db.catalogue()
                categories = cook_categories(db, claims)
                toolbox = ToolBox(
                    db,
                    claims,
                    # Bounded by the request's time left (the tool box passes it), and no
                    # retry: a second attempt could not fit inside the 30 s budget.
                    lambda text, timeout: gemini.get_embeddings(
                        text, get_gemini(), max_retries=0, timeout_secs=timeout
                    ),
                    catalogue,
                )
                toolbox.admit_answers((t.answer for t in body.history), (a for t in body.history for a in t.added))
                raw = run_agent(
                    client=client,
                    model=model,
                    system=system_blocks(catalogue),
                    user_text=request_text(
                        today=body.today,
                        measuring=body.measuring,
                        filters=list(body.filters),
                        question=body.text,
                        history=body.history,
                        categories=categories,
                    ),
                    toolbox=toolbox,
                    on_progress=lambda text: emit({"type": "progress", "text": text}),
                    cancel=cancel,
                )
                answer = finalize(raw, permitted=toolbox.permitted(raw_ids(raw)))
                kind = answer.kind
                emit({"type": "answer", "answer": _ANSWER.dump_python(answer, mode="json")})
            except AgentCancelled:
                kind = "cancelled"  # the cook has gone; nobody is left to tell
            except AgentTimeout:
                kind = "timeout"
                emit(_error("timeout", TIMEOUT_DETAIL))
            except Exception:  # noqa: BLE001  # fail-loud.bare-except-ok: after the stream began, every failure is one error line
                logger.error("search ask failed for user %s", sub, exc_info=True)
                emit(_error("failed", FAILED_DETAIL))
            finally:
                records = ctx.snapshot()
                rounds = sum(1 for r in records if r["step"] == "agent")
                tokens = sum((r["prompt_tokens"] or 0) + (r["output_tokens"] or 0) for r in records)
    finally:
        logger.info(
            "search ask user=%s rounds=%d kind=%s duration_ms=%.0f tokens=%d",
            sub,
            rounds,
            kind,
            (time.monotonic() - started) * 1000,
            tokens,
        )
        emit(None)


async def ask_stream(
    *,
    body: AskRequest,
    claims: dict[str, Any],
    db: AiReaderDb,
    client: anthropic.Anthropic,
    get_gemini: Callable[[], Any],
    get_service_client: Callable[[], Any],
    model: str,
    cancel: threading.Event | None = None,
) -> AsyncIterator[bytes]:
    """The NDJSON lines of one request. Leaving early (the cook went away) sets ``cancel``."""
    async for line in _stream(
        _work,
        WORKER_NAME,
        cancel,
        body=body,
        claims=claims,
        db=db,
        client=client,
        get_gemini=get_gemini,
        get_service_client=get_service_client,
        model=model,
    ):
        yield line


async def _stream(
    target: Callable[..., None], name: str, cancel: threading.Event | None, **kwargs: Any
) -> AsyncIterator[bytes]:
    """Run ``target(**kwargs, cancel=, emit=)`` in a worker thread and yield what it emits as NDJSON.

    The worker ends with ``emit(None)``. Leaving early (the cook went away) sets ``cancel``.
    """
    cancel = cancel if cancel is not None else threading.Event()
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def emit(item: dict[str, Any] | None) -> None:
        try:
            loop.call_soon_threadsafe(lines.put_nowait, item)
        except RuntimeError:  # the loop has closed: the response is long gone
            pass

    worker = threading.Thread(target=target, name=name, daemon=True, kwargs={**kwargs, "cancel": cancel, "emit": emit})
    worker.start()
    try:
        while (item := await lines.get()) is not None:
            yield _line(item)
    finally:
        cancel.set()


# -- POST /search/look ----------------------------------------------------------------------------


class LookRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    answer: Answer
    chosen: int = Field(ge=0, le=MAX_ALTERNATIVES - 1)
    measuring: str = Field(min_length=1, max_length=60)
    exclude: list[Annotated[str, Field(max_length=MAX_URL_CHARS)]] = Field(default_factory=list, max_length=MAX_EXCLUDE)
    scope: Literal["sites", "web"] = "sites"

    @model_validator(mode="after")
    def _has_gaps(self) -> LookRequest:
        if not self.gaps():
            raise ValueError("the answer reports no gaps to look for")
        return self

    def gaps(self) -> list[tuple[str | None, str]]:
        """The gaps looked for, each as (its group's heading or None, what is missing), in order.

        A recipes answer's chosen alternative's gaps; a none answer's gap; nothing otherwise. A
        candidate's ``gap`` is an index into this list.
        """
        answer = self.answer
        if isinstance(answer, NoneAnswer):
            return [(None, answer.gap)] if answer.gap.strip() else []
        if not isinstance(answer, RecipesAnswer) or self.chosen >= len(answer.alternatives):
            return []
        alternative = answer.alternatives[self.chosen]
        out: list[tuple[str | None, str]] = []
        for gap in alternative.gaps:
            heading = alternative.groups[gap.group].heading if 0 <= gap.group < len(alternative.groups) else None
            out.append((heading, gap.missing))
        return out


def _blocked(get_service_client: Callable[[], Any], hosts: set[str]) -> set[str]:
    """The hosts that refused an import lately; empty without a service client or on any error."""
    if not hosts:
        return set()
    sb = get_service_client()
    if sb is None:
        return set()
    try:
        return refusing_hosts.blocked_hosts(sb, hosts, datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001  # fail-loud.bare-except-ok: the record only colours the rows; it never fails a look
        logger.warning("search look: could not read refusing_hosts", exc_info=True)
        return set()


# Anthropic documents no cap on ``blocked_domains`` for the Messages API; the only documented number
# is the 64-domain cap per list for Managed Agents, so that is the conservative limit used here. The
# cook's own sites come first in the list, so they are the last to be cut.
MAX_BLOCKED_DOMAINS = 64


def _web_blocked(
    db: AiReaderDb, claims: dict[str, Any], get_service_client: Callable[[], Any]
) -> tuple[list[str], dict[str, str]]:
    """The domains the open-web look must not use, and the cook's searchable sites (host -> name).

    The cook's sites come first (a find on the open web is one they do not already have), then the
    sites Anthropic's crawler refuses, then the hosts that refused an import lately. De-duplicated
    in that order and cut to ``MAX_BLOCKED_DOMAINS``, so the cut falls on the least certain first.
    Domains the API itself refused as ``blocked_domains`` (``WEB_BLOCK_REFUSED``) are left off. A read
    error on ``refusing_hosts`` leaves that part out."""
    refused = KNOWN_UNSEARCHABLE | UNSEARCHABLE.current()
    cook = cook_sites(db, claims, skip=refused)
    lately: set[str] = set()
    sb = get_service_client()
    if sb is not None:
        try:
            lately = refusing_hosts.blocked_now(sb, datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001  # fail-loud.bare-except-ok: the record only steers the look; it never fails one
            logger.warning("search look: could not read refusing_hosts", exc_info=True)
    listable = refused - WEB_BLOCK_REFUSED.current()
    ordered = [host for host, _ in cook] + sorted(listable) + sorted(lately)
    return list(dict.fromkeys(ordered))[:MAX_BLOCKED_DOMAINS], dict(cook)


def _look_sites(
    db: AiReaderDb, claims: dict[str, Any], run: Callable[[dict[str, str]], Any]
) -> tuple[dict[str, str], Any] | str:
    """Run ``run(sites)`` over the cook's searchable sites; the result with the sites it ran on, or
    the ``no_sites`` detail.

    Sites Anthropic's crawler refuses are skipped: the known ones (``KNOWN_UNSEARCHABLE``) and the
    ones learned in this process (``UNSEARCHABLE``). When a request names one anyway
    (``UnsearchableSites``), they are remembered, the site list is read again (so the next sites take
    their places in the top 8), and the look runs again, up to ``MAX_SITE_ATTEMPTS`` requests in all:
    the refill can bring in another refused site (bug 5c6f4904, epicurious.com).
    """

    def searchable() -> list[tuple[str, str]]:
        return cook_sites(db, claims, skip=KNOWN_UNSEARCHABLE | UNSEARCHABLE.current())

    for attempt in range(1, MAX_SITE_ATTEMPTS + 1):
        sites = dict(searchable())
        if not sites:
            return NO_SEARCHABLE_SITES_DETAIL if cook_sites(db, claims) else NO_SITES_DETAIL
        try:
            return sites, run(sites)
        except UnsearchableSites as exc:
            UNSEARCHABLE.add(exc.domains)
            if not exc.domains & set(sites):
                raise  # it named none of the sites asked for: dropping nothing would not help
            if attempt == MAX_SITE_ATTEMPTS and searchable():
                raise  # out of attempts: a failure
    return NO_SEARCHABLE_SITES_DETAIL


@dataclass
class _Links:
    """The links lane of one look: the sites Brave is asked about, and its request while it runs.

    ``names`` maps each of the lane's sites that is one of the cook's to the cook's name for it, so a
    link on it shows the same site name as a find there. ``future`` is None when no request was sent
    (no client, no site in scope, or no query within Brave's limits)."""

    sites: list[str]
    names: dict[str, str] = field(default_factory=dict)
    future: concurrent.futures.Future[list[BraveResult]] | None = None
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    submitted: float = 0.0


def _start_links(brave: BraveSearch | None, body: LookRequest, db: AiReaderDb, claims: dict[str, Any]) -> _Links:
    """Send the lane's one Brave request on a worker thread, so it runs alongside the agent.

    The web look asks about the whole refused set; the sites look about the cook's own sites in it.
    The question gains " recipe" as the find's does (``look_links_query``, F-866). Nothing in scope
    (or no client) sends no request."""
    if brave is None:
        return _Links(sites=[])
    cook = cook_sites(db, claims)
    sites = links_sites(body.scope, [host for host, _ in cook])
    fitted = fit_sites(web_query(body.question), sites)
    if len(fitted) < len(sites):
        logger.warning(
            "search look: the links lane asked about %d of %d sites to fit Brave's limits", len(fitted), len(sites)
        )
    sites = fitted
    names = {host: name for host, name in cook if host in sites}
    query = look_links_query(body.question, sites) if sites else None
    if query is None:
        return _Links(sites=sites, names=names)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=LINKS_WORKER_NAME)
    submitted = time.monotonic()
    future = executor.submit(brave.search, query)
    return _Links(sites=sites, names=names, future=future, executor=executor, submitted=submitted)


def _links_results(lane: _Links) -> list[BraveResult]:
    """Brave's results, waiting no longer than ``LINKS_TIMEOUT_S`` from submission; [] on any failure."""
    if lane.future is None:
        return []
    remaining = max(0.0, LINKS_TIMEOUT_S - (time.monotonic() - lane.submitted))
    try:
        return lane.future.result(timeout=remaining)
    except concurrent.futures.TimeoutError:
        logger.warning("search look: the links lane timed out after %.1f s", LINKS_TIMEOUT_S)
    except BraveError as exc:  # its message never carries the key
        logger.warning("search look: the links lane failed: %s", exc)
    except Exception as exc:  # noqa: BLE001  # fail-loud.bare-except-ok: the lane never fails a look
        # Not a BraveError, so a bug: keep the traceback. The key cannot be in it: BraveSearch holds
        # it only in its httpx client's headers, and wraps every httpx failure in a BraveError (from
        # None) above, so no request or header text reaches this handler.
        logger.warning("search look: the links lane failed: %s", type(exc).__name__, exc_info=exc)
    return []


def _close_links(lane: _Links) -> None:
    """Abandon the lane without waiting, and record its request in the look's usage if it was sent.

    Called on the look's own thread, inside ``usage.attributed``: the worker thread does not inherit
    the usage context."""
    if lane.future is None or lane.executor is None:
        return
    lane.future.cancel()  # only stops a request that has not started
    if not lane.future.cancelled():
        usage.record_tool(LINKS_STEP, LINKS_MODEL)
    lane.executor.shutdown(wait=False, cancel_futures=True)


def _look_work(
    *,
    body: LookRequest,
    claims: dict[str, Any],
    db: AiReaderDb,
    client: anthropic.Anthropic,
    get_service_client: Callable[[], Any],
    model: str,
    limits: LookLimits,
    brave: BraveSearch | None,
    fetch_page: PageFetcher,
    cancel: threading.Event,
    emit: Callable[[dict[str, Any] | None], None],
) -> None:
    """Run the look and emit its lines; always ends with ``emit(None)``.

    With ``brave``, the links lane runs alongside the agent and its plain links follow the agent's
    finds in the one ``candidates`` line. The lane never adds an error: when the agent fails, its
    error line stands alone. Every page from either lane is then fetched with ``fetch_page`` and
    dropped when it declares no schema.org Recipe (``keep_recipe_pages``, F-866)."""
    sub = str(claims.get("sub", ""))
    started = time.monotonic()
    kind = "failed"
    calls = tokens = found = link_count = 0
    gaps = body.gaps()
    lane = _Links(sites=[])

    def run_web(blocked: list[str]) -> Any:
        return run_look(
            client=client,
            model=model,
            system=web_look_system_blocks(),
            user_text=web_look_request_text(
                measuring=body.measuring, question=body.question, gaps=gaps, exclude=list(body.exclude)
            ),
            hosts=None,
            blocked=blocked,
            on_progress=lambda text: emit({"type": "progress", "text": text}),
            cancel=cancel,
            limits=replace(limits, seconds=limits.seconds - (time.monotonic() - started)),
        )

    def run(sites: dict[str, str]) -> Any:
        return run_look(
            client=client,
            model=model,
            system=look_system_blocks(),
            user_text=look_request_text(
                sites=list(sites),
                measuring=body.measuring,
                question=body.question,
                gaps=gaps,
                exclude=list(body.exclude),
            ),
            hosts=list(sites),
            on_progress=lambda text: emit({"type": "progress", "text": text}),
            cancel=cancel,
            limits=replace(limits, seconds=limits.seconds - (time.monotonic() - started)),
        )

    try:
        with usage.attributed(sub, "search", get_client=get_service_client) as ctx:
            try:
                # The lane's sites are worked out once, from the refused set as it stands before the
                # agent starts. Sites the sites look learns are refused mid-request (UnsearchableSites,
                # then a retry) are not added: the lane's one request has already gone.
                lane = _start_links(brave, body, db, claims)
                if body.scope == "web":
                    blocked_list, cook = _web_blocked(db, claims, get_service_client)
                    try:
                        raw, fetched = run_web(blocked_list)
                    except UnsearchableSites as exc:
                        # The API may refuse a site as a blocked domain too: remember it for both lists, and
                        # run once more without those domains, inside the same limits. A second refusal fails.
                        UNSEARCHABLE.add(exc.domains)
                        WEB_BLOCK_REFUSED.add(exc.domains)
                        retry = [d for d in blocked_list if d not in exc.domains]
                        if len(retry) == len(blocked_list):
                            raise  # it named none of them: dropping nothing would not help
                        try:
                            raw, fetched = run_web(retry)
                        except UnsearchableSites as again:
                            UNSEARCHABLE.add(again.domains)
                            WEB_BLOCK_REFUSED.add(again.domains)
                            raise
                    outcome: Any = (cook, (raw, fetched))
                    hosts: set[str] | None = None
                else:
                    outcome = _look_sites(db, claims, run)
                    hosts = None if isinstance(outcome, str) else set(outcome[0])
                if isinstance(outcome, str) and lane.future is None:
                    kind = "no_sites"  # neither the agent nor the links lane has a site
                    emit(_error("no_sites", outcome))
                else:
                    sites: dict[str, str] = {}
                    checked: list[RawCandidate] = []
                    if not isinstance(outcome, str):
                        sites, (raw, fetched) = outcome
                        checked = check_candidates(
                            raw, hosts=hosts, fetched=fetched, exclude=set(body.exclude), gap_count=len(gaps)
                        )
                    plain = to_links(
                        _links_results(lane),
                        sites=lane.sites,
                        exclude=body.exclude,
                        offered=[c.url for c in checked],
                    )
                    if lane.future is not None and cancel.is_set():
                        raise AgentCancelled  # the cook left while the lane was awaited
                    links_offered = set(plain)
                    checked = keep_recipe_pages([*checked, *plain], fetch=fetch_page)
                    if cancel.is_set():
                        raise AgentCancelled  # the cook left while the pages were read
                    link_count = sum(1 for c in checked if c in links_offered)
                    own = own_source_urls(db, claims, [c.url for c in checked]) if checked else {}
                    blocked = _blocked(get_service_client, {url_host(c.url) for c in checked})
                    # The cook's names for the lane's sites too: the cook never sees which search found a page.
                    named = {**lane.names, **sites}
                    candidates = classify(checked, sites=named, own=own, blocked=blocked)
                    kind, found = "candidates", len(candidates)
                    emit({"type": "candidates", "candidates": candidates})
            except AgentCancelled:
                kind = "cancelled"  # the cook has gone; nobody is left to tell
            except AgentTimeout:
                kind = "timeout"
                emit(_error("timeout", TIMEOUT_DETAIL))
            except Exception:  # noqa: BLE001  # fail-loud.bare-except-ok: after the stream began, every failure is one error line
                logger.error("search look failed for user %s", sub, exc_info=True)
                emit(_error("failed", FAILED_DETAIL))
            finally:
                _close_links(lane)  # never waits; records the request on this thread
                records = ctx.snapshot()
                calls = sum(1 for r in records if r["step"] == "look")
                tokens = sum((r["prompt_tokens"] or 0) + (r["output_tokens"] or 0) for r in records)
    finally:
        logger.info(
            "search look user=%s calls=%d kind=%s found=%d links=%d duration_ms=%.0f tokens=%d",
            sub,
            calls,
            kind,
            found,
            link_count,
            (time.monotonic() - started) * 1000,
            tokens,
        )
        emit(None)


async def look_stream(
    *,
    body: LookRequest,
    claims: dict[str, Any],
    db: AiReaderDb,
    client: anthropic.Anthropic,
    get_service_client: Callable[[], Any],
    model: str,
    limits: LookLimits = LookLimits(),
    cancel: threading.Event | None = None,
    brave: BraveSearch | None = None,
    fetch_page: PageFetcher = fetch_page_html,
) -> AsyncIterator[bytes]:
    """The NDJSON lines of one look. Leaving early (the cook went away) sets ``cancel``. Without
    ``brave`` the look shows no plain links. ``fetch_page`` reads each candidate page for the
    recipe check (a fake in tests)."""
    async for line in _stream(
        _look_work,
        LOOK_WORKER_NAME,
        cancel,
        body=body,
        claims=claims,
        db=db,
        client=client,
        get_service_client=get_service_client,
        model=model,
        limits=limits,
        brave=brave,
        fetch_page=fetch_page,
    ):
        yield line


# -- POST /search/find ----------------------------------------------------------------------------


class FindRequest(BaseModel):
    query: str
    exclude: list[Annotated[str, Field(max_length=MAX_URL_CHARS)]] = Field(default_factory=list, max_length=MAX_EXCLUDE)

    @field_validator("query")
    @classmethod
    def _stripped_query(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= MAX_QUERY_CHARS:
            raise ValueError(f"the query must be 1 to {MAX_QUERY_CHARS} characters")
        return value


def _find_result(
    future: concurrent.futures.Future[list[BraveResult]] | None, group: str, submitted: float
) -> list[BraveResult] | None:
    """One request's results, waiting no longer than ``FIND_TIMEOUT_S`` from submission; None when it
    failed or timed out (and when it was never sent). The query is never logged."""
    if future is None:
        return None
    remaining = max(0.0, FIND_TIMEOUT_S - (time.monotonic() - submitted))
    try:
        return future.result(timeout=remaining)
    except concurrent.futures.TimeoutError:
        logger.warning("search find: the %s request timed out after %.1f s", group, FIND_TIMEOUT_S)
    except BraveError as exc:  # its message never carries the key or the query
        logger.warning("search find: the %s request failed: %s", group, exc)
    except Exception as exc:  # noqa: BLE001  # fail-loud.bare-except-ok: one request failing empties only its group
        logger.warning("search find: the %s request failed: %s", group, type(exc).__name__)
    return None


def _find_work(
    *,
    body: FindRequest,
    claims: dict[str, Any],
    db: AiReaderDb,
    brave: BraveSearch,
    get_service_client: Callable[[], Any],
) -> dict[str, Any]:
    """Ask Brave about the cook's sites and the open web at once and build the two groups of rows.

    Returns ``{"sites": [...], "web": [...], "sites_asked": bool}``; a group whose request failed is
    empty. Raises 502 when every request sent failed. Each request sent is one ``ai_usage`` row."""
    sub = str(claims.get("sub", ""))
    started = time.monotonic()
    sites_rows: list[dict[str, Any]] = []
    web_rows: list[dict[str, Any]] = []
    requests = 0
    try:
        with usage.attributed(sub, "search", get_client=get_service_client):
            cook = cook_sites(db, claims)
            cook_hosts = [host for host, _ in cook]
            sites_q = sites_query(body.query, cook_hosts)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix=FIND_WORKER_NAME)
            try:
                submitted = time.monotonic()
                sites_future = executor.submit(brave.search, sites_q) if sites_q is not None else None
                web_future = executor.submit(brave.search, web_query(body.query))
                sites_results = _find_result(sites_future, "sites", submitted)
                web_results = _find_result(web_future, "web", submitted)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            # Recorded here, on the request's own thread: the workers do not inherit the usage context.
            requests = 1 if sites_future is None else 2
            for _ in range(requests):
                usage.record_tool(FIND_STEP, LINKS_MODEL)
            if sites_results is None and web_results is None:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=FIND_FAILED_DETAIL)

            seen: set[str] = set()
            for url in body.exclude:
                try:
                    seen.add(normalise_url(url))
                except ValueError:
                    continue
            picked_sites = pick(
                sites_results or [], hosts=set(cook_hosts), seen=seen, limit=SITES_MAX, recipes_first=True
            )
            picked_web = pick(web_results or [], hosts=None, seen=seen, limit=WEB_MAX)
            picked = picked_sites + picked_web
            if picked:
                own = own_source_urls(db, claims, [c.url for c in picked])
                blocked = _blocked(get_service_client, {url_host(c.url) for c in picked})
                # Brave's names for any site, then the cook's own names for theirs.
                names = {**site_names([*(sites_results or []), *(web_results or [])]), **dict(cook)}
                rows = classify(picked, sites=names, own=own, blocked=blocked)
                for row in rows:
                    row.pop("gap", None)
                sites_rows, web_rows = rows[: len(picked_sites)], rows[len(picked_sites) :]
            return {"sites": sites_rows, "web": web_rows, "sites_asked": sites_future is not None}
    finally:
        logger.info(
            "search find user=%s sites=%d web=%d requests=%d duration_ms=%.0f",
            sub,
            len(sites_rows),
            len(web_rows),
            requests,
            (time.monotonic() - started) * 1000,
        )


def build_router(
    verify_jwt: Callable[..., Any],
    gate: AiGate,
    get_db: Callable[[], AiReaderDb | None],
    get_anthropic: Callable[[], anthropic.Anthropic | None],
    get_gemini: Callable[[], Any],
    get_service_client: Callable[[], Any],
    model: str,
    get_brave: Callable[[], BraveSearch | None],
    *,
    fetch_page: PageFetcher = fetch_page_html,
) -> APIRouter:
    router = APIRouter()

    # A plain def: FastAPI runs it on the thread pool, where the gate's database reads belong.
    @router.post("/search/ask", response_class=StreamingResponse)
    def ask(body: AskRequest, claims: dict[str, Any] = Depends(verify_jwt)) -> StreamingResponse:
        db = get_db()
        client = get_anthropic()
        if db is None or client is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED_DETAIL)
        gate.check(str(claims.get("sub", "")), search=True)  # 503 / 429; the slot is spent here
        return StreamingResponse(
            ask_stream(
                body=body,
                claims=dict(claims),
                db=db,
                client=client,
                get_gemini=get_gemini,
                get_service_client=get_service_client,
                model=model,
            ),
            media_type=MEDIA_TYPE,
        )

    @router.post("/search/look", response_class=StreamingResponse)
    def look(body: LookRequest, claims: dict[str, Any] = Depends(verify_jwt)) -> StreamingResponse:
        db = get_db()
        client = get_anthropic()
        if db is None or client is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED_DETAIL)
        gate.check(str(claims.get("sub", "")), search=True)  # 503 / 429; a look spends one search slot
        return StreamingResponse(
            look_stream(
                body=body,
                claims=dict(claims),
                db=db,
                client=client,
                get_service_client=get_service_client,
                model=model,
                brave=get_brave(),  # None (no key) only turns the plain links off; never a 503
                fetch_page=fetch_page,
            ),
            media_type=MEDIA_TYPE,
        )

    # JSON, not a stream: no model runs, and both Brave requests end within FIND_TIMEOUT_S.
    @router.post("/search/find")
    def find(body: FindRequest, claims: dict[str, Any] = Depends(verify_jwt)) -> dict[str, Any]:
        db = get_db()
        if db is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED_DETAIL)
        brave = get_brave()
        if brave is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=FIND_UNSET_DETAIL)
        gate.check(str(claims.get("sub", "")), find=True)  # 503 / 429; a find spends one find slot
        return _find_work(body=body, claims=dict(claims), db=db, brave=brave, get_service_client=get_service_client)

    return router
