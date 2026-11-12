# recipeparser/adapters/ai_search/agent.py
"""The agent loop behind ``POST /search/ask`` (Cayenne AI search §7.7, plan D4).

A manual loop, not the SDK's tool runner, because it must enforce the limits itself: at most
``rounds`` tool rounds, a ``seconds`` wall clock across the whole request, and a cancel flag checked
before every model call and every tool run. A model call is not started with less than
``MIN_CALL_SECONDS`` left, and the SDK's own retries are off (``max_retries=0``) so one call cannot
outlast the time it was given. Each call's usage is recorded as step ``agent``. The final answer is
the structured-output text block of the ``end_turn`` response, parsed as ``RawAnswer``.

``PoolTimeout`` and ``NotAiReader`` raised by the tool box propagate unchanged; the endpoint maps
them (infrastructure failures, not something the agent can recover from).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
from pydantic import ValidationError

from recipeparser import usage
from recipeparser.adapters.ai_search.answer import RAW_ANSWER_SCHEMA, RawAnswer
from recipeparser.adapters.ai_search.tools import TOOL_DEFINITIONS, ToolOutcome

logger = logging.getLogger(__name__)

BETAS = ["task-budgets-2026-03-13", "server-side-fallback-2026-07-01"]
MIN_CALL_SECONDS = 1.5
FINAL_NOTICE = "You have used all your searches. Answer now from what you found."


class AgentTimeout(Exception):
    """The wall clock ran out before an answer came back."""


class AgentFailed(Exception):
    """The agent stopped without a usable answer (refusal, cut off, bad JSON, API error)."""


class AgentCancelled(Exception):
    """The cook left; the loop stopped before its next model call."""


@dataclass(frozen=True)
class AgentLimits:
    rounds: int = 10
    seconds: float = 30.0
    task_budget: int = 40_000
    max_tokens: int = 16_000


class _ToolRunner(Protocol):
    def run(self, name: str, tool_input: Any) -> ToolOutcome: ...


def _output_config(limits: AgentLimits) -> dict[str, Any]:
    return {
        "effort": "medium",
        "task_budget": {"type": "tokens", "total": limits.task_budget},
        "format": {"type": "json_schema", "schema": RAW_ANSWER_SCHEMA},
    }


def _parse_answer(response: Any) -> RawAnswer:
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise AgentFailed("the final response carried no text block")
    try:
        return RawAnswer.model_validate_json(text)
    except ValidationError as exc:
        raise AgentFailed(f"the final answer did not match the schema: {exc.error_count()} error(s)") from exc


def _check(cancel: threading.Event, deadline: float, clock: Callable[[], float], margin: float) -> float:
    """Raise if the cook left or fewer than ``margin`` seconds remain; return the seconds left."""
    if cancel.is_set():
        raise AgentCancelled()
    remaining = deadline - clock()
    if remaining <= margin:
        raise AgentTimeout()
    return remaining


def _tool_results(
    response: Any,
    toolbox: _ToolRunner,
    on_progress: Callable[[str], None],
    check: Callable[[], object],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        check()
        outcome = toolbox.run(block.name, block.input)
        if outcome.progress:
            on_progress(outcome.progress)
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": outcome.content,
                "is_error": outcome.is_error,
            }
        )
    return results


def run_agent(
    *,
    client: anthropic.Anthropic,
    model: str,
    system: list[dict[str, Any]],
    user_text: str,
    toolbox: _ToolRunner,
    on_progress: Callable[[str], None],
    cancel: threading.Event,
    limits: AgentLimits = AgentLimits(),
    clock: Callable[[], float] = time.monotonic,
) -> RawAnswer:
    """Drive the model through its tools until it answers; raise on timeout, failure or cancel."""
    deadline = clock() + limits.seconds
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
    rounds = 0
    final = False
    while True:
        remaining = _check(cancel, deadline, clock, MIN_CALL_SECONDS)
        extra: dict[str, Any] = {"tool_choice": {"type": "none"}} if final else {}
        try:
            # The request dicts are plain dicts, not the SDK TypedDicts, so no overload matches statically.
            response = client.with_options(max_retries=0).beta.messages.create(  # type: ignore[call-overload]
                model=model,
                max_tokens=limits.max_tokens,
                system=system,
                tools=TOOL_DEFINITIONS,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config=_output_config(limits),
                betas=BETAS,
                fallbacks="default",
                timeout=remaining,
                **extra,
            )
        except anthropic.APITimeoutError as exc:
            raise AgentTimeout() from exc
        except anthropic.APIError as exc:
            logger.warning("search agent: Anthropic call failed: %s", type(exc).__name__)
            raise AgentFailed(type(exc).__name__) from exc

        usage.record_claude("agent", response.model, response.usage)
        messages.append({"role": "assistant", "content": response.content})
        stop = response.stop_reason

        if stop == "end_turn":
            return _parse_answer(response)
        if final or stop not in ("tool_use", "pause_turn"):
            if stop == "refusal":
                logger.warning("search agent: refusal %s", getattr(response, "stop_details", None))
            raise AgentFailed(f"stopped with {stop}")

        if stop == "tool_use":
            messages.append({"role": "user", "content": _tool_results(
                response, toolbox, on_progress, lambda: _check(cancel, deadline, clock, 0.0)
            )})
        rounds += 1
        if rounds >= limits.rounds:
            final = True
            if stop == "tool_use":
                # A system message may follow a user message; after pause_turn the last turn is the
                # assistant's, so only tool_choice none forces the answer there.
                messages.append({"role": "system", "content": FINAL_NOTICE})
