"""Who an AI call is for (AI caps spec D8).

A ContextVar carries the user, the action and the job. It is set where each
endpoint and worker starts, and every Gemini call records its token counts into
it. Records are written to ai_usage in one insert when the request or job ends.
A thread pool does not inherit a context: a submit that makes Gemini calls must
go through contextvars.copy_context().run (core/pipeline.py does). Writing usage
never fails the user's work.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional

log = logging.getLogger(__name__)

Action = Literal["import", "regen", "recat", "picture", "classify", "search"]


@dataclass
class UsageContext:
    user_id: str
    action: Action
    job_id: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


ai_context: ContextVar[Optional[UsageContext]] = ContextVar("ai_context", default=None)


def _tokens(usage_metadata: object | None, name: str) -> Optional[int]:
    value = getattr(usage_metadata, name, None) if usage_metadata is not None else None
    return value if isinstance(value, int) else None


def record(step: str, model: str, usage_metadata: object | None) -> None:
    """Add one call to the current context's buffer; outside one, nothing is kept."""
    ctx = ai_context.get()
    if ctx is None:
        return
    row = {
        "user_id": ctx.user_id,
        "action": ctx.action,
        "step": step,
        "job_id": ctx.job_id,
        "recipe_id": None,
        "model": model,
        "calls": 1,
        "prompt_tokens": _tokens(usage_metadata, "prompt_token_count"),
        "output_tokens": _tokens(usage_metadata, "candidates_token_count"),
        "thought_tokens": _tokens(usage_metadata, "thoughts_token_count"),
    }
    with ctx._lock:
        ctx.records.append(row)


def flush(ctx: UsageContext, get_client: Callable[[], Any]) -> None:
    """Write the buffer to ai_usage in one insert, never raising."""
    with ctx._lock:
        rows, ctx.records = ctx.records, []
    if not rows:
        return
    try:
        sb = get_client()
        if sb is None:
            log.warning("usage: no database configured; %d call record(s) not written.", len(rows))
            return
        sb.table("ai_usage").insert(rows).execute()
    except Exception:  # noqa: BLE001 - usage never fails the user's work
        log.exception("usage: could not write %d call record(s) for %s/%s.", len(rows), ctx.action, ctx.user_id)


@contextmanager
def attributed(
    user_id: str,
    action: Action,
    job_id: Optional[str] = None,
    *,
    get_client: Callable[[], Any],
) -> Iterator[UsageContext]:
    ctx = UsageContext(user_id=user_id, action=action, job_id=job_id)
    token = ai_context.set(ctx)
    try:
        yield ctx
    finally:
        try:
            flush(ctx, get_client)
        finally:
            ai_context.reset(token)
