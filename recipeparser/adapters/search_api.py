"""POST /search/ask: the agent behind a deeper Library search, streamed (Cayenne AI search §7.1).
POST /search/look: the look on the cook's own sites (``scope`` "sites", the default) or on the open
web (``scope`` "web") for an answer's gaps (Stage 5, §7.2).

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
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter, model_validator

from recipeparser import usage
from recipeparser.adapters import refusing_hosts
from recipeparser.adapters.ai_gate import AiGate
from recipeparser.adapters.ai_search.agent import AgentCancelled, AgentTimeout, run_agent
from recipeparser.adapters.ai_search.ai_db import AiReaderDb, cook_sites, own_source_urls
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
from recipeparser.adapters.ai_search.look import (
    KNOWN_UNSEARCHABLE,
    MAX_URL_CHARS,
    UNSEARCHABLE,
    LookLimits,
    UnsearchableSites,
    check_candidates,
    classify,
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
from recipeparser.adapters.ai_search.tools import ToolBox

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
                toolbox.admit_answers(
                    (t.answer for t in body.history), (a for t in body.history for a in t.added)
                )
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
            except Exception:  # noqa: BLE001 - after the stream began, every failure is one error line
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
    except Exception:  # noqa: BLE001 - the record only colours the rows; it never fails a look
        logger.warning("search look: could not read refusing_hosts", exc_info=True)
        return set()


# Anthropic documents no limit for ``blocked_domains`` (neither the SDK's parameter docs nor the tool
# pages give one), so this is a conservative cap that keeps the request small. The cook's own sites
# come first in the list, so they are the last to be cut.
MAX_BLOCKED_DOMAINS = 100


def _web_blocked(
    db: AiReaderDb, claims: dict[str, Any], get_service_client: Callable[[], Any]
) -> tuple[list[str], dict[str, str]]:
    """The domains the open-web look must not use, and the cook's searchable sites (host -> name).

    The cook's sites come first (a find on the open web is one they do not already have), then the
    hosts that refused an import lately, then the sites Anthropic's crawler refuses. De-duplicated
    in that order and cut to ``MAX_BLOCKED_DOMAINS``. A read error on ``refusing_hosts`` leaves that
    part out."""
    refused = KNOWN_UNSEARCHABLE | UNSEARCHABLE.current()
    cook = cook_sites(db, claims, skip=refused)
    lately: set[str] = set()
    sb = get_service_client()
    if sb is not None:
        try:
            lately = refusing_hosts.blocked_now(sb, datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001 - the record only steers the look; it never fails one
            logger.warning("search look: could not read refusing_hosts", exc_info=True)
    ordered = [host for host, _ in cook] + sorted(lately) + sorted(refused)
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


def _look_work(
    *,
    body: LookRequest,
    claims: dict[str, Any],
    db: AiReaderDb,
    client: anthropic.Anthropic,
    get_service_client: Callable[[], Any],
    model: str,
    limits: LookLimits,
    cancel: threading.Event,
    emit: Callable[[dict[str, Any] | None], None],
) -> None:
    """Run the look and emit its lines; always ends with ``emit(None)``."""
    sub = str(claims.get("sub", ""))
    started = time.monotonic()
    kind = "failed"
    calls = tokens = found = 0
    gaps = body.gaps()

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
                if body.scope == "web":
                    blocked_list, cook = _web_blocked(db, claims, get_service_client)
                    try:
                        raw, fetched = run_web(blocked_list)
                    except UnsearchableSites as exc:
                        UNSEARCHABLE.add(exc.domains)  # the next look steers off them
                        raise
                    outcome: Any = (cook, (raw, fetched))
                    hosts: set[str] | None = None
                else:
                    outcome = _look_sites(db, claims, run)
                    hosts = None if isinstance(outcome, str) else set(outcome[0])
                if isinstance(outcome, str):
                    kind = "no_sites"
                    emit(_error("no_sites", outcome))
                else:
                    sites, (raw, fetched) = outcome
                    checked = check_candidates(
                        raw, hosts=hosts, fetched=fetched, exclude=set(body.exclude), gap_count=len(gaps)
                    )
                    own = own_source_urls(db, claims, [c.url for c in checked]) if checked else {}
                    blocked = _blocked(get_service_client, {url_host(c.url) for c in checked})
                    candidates = classify(checked, sites=sites, own=own, blocked=blocked)
                    kind, found = "candidates", len(candidates)
                    emit({"type": "candidates", "candidates": candidates})
            except AgentCancelled:
                kind = "cancelled"  # the cook has gone; nobody is left to tell
            except AgentTimeout:
                kind = "timeout"
                emit(_error("timeout", TIMEOUT_DETAIL))
            except Exception:  # noqa: BLE001 - after the stream began, every failure is one error line
                logger.error("search look failed for user %s", sub, exc_info=True)
                emit(_error("failed", FAILED_DETAIL))
            finally:
                records = ctx.snapshot()
                calls = sum(1 for r in records if r["step"] == "look")
                tokens = sum((r["prompt_tokens"] or 0) + (r["output_tokens"] or 0) for r in records)
    finally:
        logger.info(
            "search look user=%s calls=%d kind=%s found=%d duration_ms=%.0f tokens=%d",
            sub,
            calls,
            kind,
            found,
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
) -> AsyncIterator[bytes]:
    """The NDJSON lines of one look. Leaving early (the cook went away) sets ``cancel``."""
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
    ):
        yield line


def build_router(
    verify_jwt: Callable[..., Any],
    gate: AiGate,
    get_db: Callable[[], AiReaderDb | None],
    get_anthropic: Callable[[], anthropic.Anthropic | None],
    get_gemini: Callable[[], Any],
    get_service_client: Callable[[], Any],
    model: str,
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
            ),
            media_type=MEDIA_TYPE,
        )

    return router
