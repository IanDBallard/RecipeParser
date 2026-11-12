"""POST /search/ask: the agent behind a deeper Library search, streamed (Cayenne AI search §7.1).

Refusals before the agent starts are ordinary JSON errors: 401 (token), 422 (body), 503 (not
configured, or paused) and 429 (the search rate limit, with Retry-After). The gate's slot is spent
immediately before the stream starts. After that the response is NDJSON (plan D5): progress lines,
then exactly one ``answer`` line or one ``error`` line, never an HTML 500.

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
from datetime import date
from typing import Annotated, Any

import anthropic
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter

from recipeparser import usage
from recipeparser.adapters.ai_gate import AiGate
from recipeparser.adapters.ai_search.agent import AgentCancelled, AgentTimeout, run_agent
from recipeparser.adapters.ai_search.ai_db import AiReaderDb
from recipeparser.adapters.ai_search.answer import Answer, Previous, finalize, raw_ids
from recipeparser.adapters.ai_search.prompt import request_text, system_blocks
from recipeparser.adapters.ai_search.tools import ToolBox

logger = logging.getLogger(__name__)

NOT_CONFIGURED_DETAIL = "Searching more deeply isn't available right now."
TIMEOUT_DETAIL = "That took too long to answer. Try again."
FAILED_DETAIL = "Couldn't answer that just now. Try again."
MEDIA_TYPE = "application/x-ndjson"
WORKER_NAME = "search-ask"

_ANSWER: TypeAdapter[Answer] = TypeAdapter(Answer)


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    today: date  # the device's local date
    measuring: str = Field(min_length=1, max_length=60)  # D1, e.g. "metric, °C"
    filters: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=12)  # D1
    previous: Previous | None = None


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
                    lambda text: gemini.get_embeddings(text, get_gemini(), max_retries=1),
                    catalogue,
                )
                if body.previous is not None:
                    toolbox.admit_previous(body.previous.answer)
                raw = run_agent(
                    client=client,
                    model=model,
                    system=system_blocks(catalogue),
                    user_text=request_text(
                        today=body.today,
                        measuring=body.measuring,
                        filters=list(body.filters),
                        question=body.text,
                        previous=body.previous,
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
                with ctx._lock:
                    rounds = sum(1 for r in ctx.records if r["step"] == "agent")
                    tokens = sum((r["prompt_tokens"] or 0) + (r["output_tokens"] or 0) for r in ctx.records)
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
    cancel = cancel if cancel is not None else threading.Event()
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def emit(item: dict[str, Any] | None) -> None:
        try:
            loop.call_soon_threadsafe(lines.put_nowait, item)
        except RuntimeError:  # the loop has closed: the response is long gone
            pass

    worker = threading.Thread(
        target=_work,
        name=WORKER_NAME,
        daemon=True,
        kwargs={
            "body": body,
            "claims": claims,
            "db": db,
            "client": client,
            "get_gemini": get_gemini,
            "get_service_client": get_service_client,
            "model": model,
            "cancel": cancel,
            "emit": emit,
        },
    )
    worker.start()
    try:
        while (item := await lines.get()) is not None:
            yield _line(item)
    finally:
        cancel.set()


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

    return router
