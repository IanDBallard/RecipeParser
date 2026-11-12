"""The agent loop (Cayenne AI search §7.7). A scripted fake client: no Anthropic call, no network."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from recipeparser import usage
from recipeparser.adapters.ai_search.agent import (
    AgentCancelled,
    AgentFailed,
    AgentLimits,
    AgentTimeout,
    run_agent,
)
from recipeparser.adapters.ai_search.answer import RAW_ANSWER_SCHEMA, RawAnswer
from recipeparser.adapters.ai_search.tools import TOOL_DEFINITIONS, ToolOutcome

MODEL = "claude-opus-5-5"
SYSTEM = [{"type": "text", "text": "instructions", "cache_control": {"type": "ephemeral"}}]
FINAL = {"kind": "text", "text": "Yes, three.", "recipe_ids": [], "alternatives": []}
FINAL_SYSTEM = "You have used all your searches. Answer now from what you found."


def _usage(i: int = 100, o: int = 20, cc: int | None = 30, cr: int | None = 50) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cc, cache_read_input_tokens=cr)


def _tool_use(tid: str, name: str = "keyword_search", tool_input: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=tool_input or {"text": "soup", "limit": 5})


def _response(stop: str, content: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop, content=content, model=MODEL, usage=_usage())


def _tools(*blocks: SimpleNamespace) -> SimpleNamespace:
    return _response("tool_use", [SimpleNamespace(type="thinking", thinking="hm", signature="sig"), *blocks])


def _answer(payload: Any = None) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload or FINAL)
    return _response("end_turn", [SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, script: list[Any], on_call: Callable[[int], None] | None = None) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.on_call = on_call
        self.options: list[dict[str, Any]] = []
        self._beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    @property
    def beta(self) -> Any:
        raise AssertionError("every call must go through with_options(max_retries=0)")

    def with_options(self, **options: Any) -> Any:
        self.options.append(options)
        return SimpleNamespace(beta=self._beta)

    def _create(self, **kwargs: Any) -> Any:
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs["messages"])
        self.calls.append(snapshot)
        if self.on_call:
            self.on_call(len(self.calls))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeToolBox:
    def __init__(self, on_run: Callable[[str], None] | None = None, error_for: set[str] | None = None) -> None:
        self.runs: list[tuple[str, Mapping[str, Any]]] = []
        self.on_run = on_run
        self.error_for = error_for or set()

    def run(self, name: str, tool_input: Mapping[str, Any]) -> ToolOutcome:
        self.runs.append((name, tool_input))
        if self.on_run:
            self.on_run(name)
        if name in self.error_for:
            return ToolOutcome("That statement was refused.", True, None)
        text = tool_input.get("text")
        return ToolOutcome(json.dumps({"rows": [], "for": text}), False, f"Looking for {text}")


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _run(client: FakeClient, toolbox: Any = None, **kwargs: Any) -> RawAnswer:
    params: dict[str, Any] = {
        "client": client,
        "model": MODEL,
        "system": SYSTEM,
        "user_text": "Question: soups?",
        "toolbox": toolbox or FakeToolBox(),
        "on_progress": lambda _p: None,
        "cancel": threading.Event(),
        "clock": Clock(),
    }
    params.update(kwargs)
    return run_agent(**params)


def test_answers_after_tool_round() -> None:
    client = FakeClient([_tools(_tool_use("t1")), _answer()])
    toolbox = FakeToolBox()
    answer = _run(client, toolbox)
    assert answer == RawAnswer.model_validate(FINAL)
    assert toolbox.runs == [("keyword_search", {"text": "soup", "limit": 5})]
    second = client.calls[1]["messages"]
    assert second[0] == {"role": "user", "content": "Question: soups?"}
    assert second[1]["role"] == "assistant"
    assert second[1]["content"][0].type == "thinking"  # passed back whole, unchanged
    results = second[2]
    assert results["role"] == "user"
    assert results["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": json.dumps({"rows": [], "for": "soup"}),
            "is_error": False,
        }
    ]


def test_request_parameters() -> None:
    client = FakeClient([_answer()])
    _run(client, clock=Clock(100.0))
    call = client.calls[0]
    assert call["model"] == MODEL
    assert call["max_tokens"] == 16_000
    assert call["system"] == SYSTEM
    assert call["tools"] is TOOL_DEFINITIONS
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {
        "effort": "medium",
        "task_budget": {"type": "tokens", "total": 40_000},
        "format": {"type": "json_schema", "schema": RAW_ANSWER_SCHEMA},
    }
    assert call["betas"] == ["task-budgets-2026-03-13", "server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["timeout"] == pytest.approx(30.0)
    assert "tool_choice" not in call


def test_parallel_tool_results_in_one_message() -> None:
    client = FakeClient([_tools(_tool_use("t1"), _tool_use("t2", "run_sql", {"query": "select 1"})), _answer()])
    toolbox = FakeToolBox(error_for={"run_sql"})
    _run(client, toolbox)
    messages = client.calls[1]["messages"]
    assert len(messages) == 3
    results = messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert [r["is_error"] for r in results] == [False, True]
    assert results[1]["content"] == "That statement was refused."


def test_round_cap_forces_answer() -> None:
    script: list[Any] = [_tools(_tool_use(f"t{i}")) for i in range(10)] + [_answer()]
    client = FakeClient(script)
    answer = _run(client)
    assert answer.kind == "text"
    assert len(client.calls) == 11
    assert all("tool_choice" not in c for c in client.calls[:10])
    last = client.calls[10]
    assert last["tool_choice"] == {"type": "none"}
    assert last["messages"][-1] == {"role": "system", "content": FINAL_SYSTEM}
    assert last["messages"][-2]["role"] == "user"
    assert last["messages"][-2]["content"][0]["tool_use_id"] == "t9"


def test_round_cap_tool_use_after_final_call_fails() -> None:
    script: list[Any] = [_tools(_tool_use(f"t{i}")) for i in range(11)]
    client = FakeClient(script)
    with pytest.raises(AgentFailed):
        _run(client)
    assert len(client.calls) == 11


def test_pause_turn_counts_a_round_and_continues() -> None:
    paused = _response("pause_turn", [SimpleNamespace(type="thinking", thinking="", signature="s")])
    client = FakeClient([paused, _answer()])
    _run(client, limits=AgentLimits(rounds=3))
    assert len(client.calls) == 2
    assert client.calls[1]["messages"][-1]["role"] == "assistant"


def test_deadline_raises_timeout() -> None:
    clock = Clock(0.0)

    def advance(_name: str) -> None:
        clock.now = 31.0

    client = FakeClient([_tools(_tool_use("t1")), _answer()])
    with pytest.raises(AgentTimeout):
        _run(client, FakeToolBox(on_run=advance), clock=clock)
    assert len(client.calls) == 1


def test_timeout_kwarg_is_the_remaining_time() -> None:
    clock = Clock(0.0)

    def advance(_name: str) -> None:
        clock.now = 12.5

    client = FakeClient([_tools(_tool_use("t1")), _answer()])
    _run(client, FakeToolBox(on_run=advance), clock=clock)
    assert client.calls[0]["timeout"] == pytest.approx(30.0)
    assert client.calls[1]["timeout"] == pytest.approx(17.5)


def test_api_timeout_becomes_agent_timeout() -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    client = FakeClient([anthropic.APITimeoutError(request=request)])
    with pytest.raises(AgentTimeout):
        _run(client)


def test_api_error_becomes_agent_failed() -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    client = FakeClient([anthropic.APIConnectionError(request=request)])
    with pytest.raises(AgentFailed):
        _run(client)
    response = httpx.Response(529, request=request)
    client = FakeClient([anthropic.APIStatusError("overloaded", response=response, body=None)])
    with pytest.raises(AgentFailed):
        _run(client)


def test_cancel_stops_before_next_call() -> None:
    cancel = threading.Event()
    client = FakeClient([_tools(_tool_use("t1")), _answer()])
    with pytest.raises(AgentCancelled):
        _run(client, FakeToolBox(on_run=lambda _n: cancel.set()), cancel=cancel)
    assert len(client.calls) == 1


def test_cancel_before_first_call() -> None:
    cancel = threading.Event()
    cancel.set()
    client = FakeClient([_answer()])
    with pytest.raises(AgentCancelled):
        _run(client, cancel=cancel)
    assert client.calls == []


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "stop_sequence"])
def test_refusal_and_max_tokens_fail(stop: str) -> None:
    client = FakeClient([_response(stop, [SimpleNamespace(type="text", text="")])])
    with pytest.raises(AgentFailed):
        _run(client)


@pytest.mark.parametrize(
    "payload",
    ["not json", json.dumps({"kind": "text"}), json.dumps({**FINAL, "extra": 1}), json.dumps({**FINAL, "kind": "sql"})],
)
def test_invalid_final_json_fails(payload: str) -> None:
    client = FakeClient([_answer(payload)])
    with pytest.raises(AgentFailed):
        _run(client)


def test_end_turn_without_text_fails() -> None:
    client = FakeClient([_response("end_turn", [SimpleNamespace(type="thinking", thinking="", signature="s")])])
    with pytest.raises(AgentFailed):
        _run(client)


def test_usage_recorded_per_call() -> None:
    client = FakeClient([_tools(_tool_use("t1")), _answer()])
    with usage.attributed("cook-a", "search", get_client=lambda: None) as ctx:
        _run(client)
        rows = list(ctx.records)
    assert len(rows) == 2
    for row in rows:
        assert row["action"] == "search"
        assert row["step"] == "agent"
        assert row["model"] == MODEL
        assert row["prompt_tokens"] == 180
        assert row["output_tokens"] == 20
        assert row["thought_tokens"] is None


def test_usage_recorded_even_when_the_call_fails_after() -> None:
    client = FakeClient([_response("refusal", [])])
    with usage.attributed("cook-a", "search", get_client=lambda: None) as ctx:
        with pytest.raises(AgentFailed):
            _run(client)
        assert len(ctx.records) == 1


def test_progress_forwarded() -> None:
    seen: list[str] = []

    class Mixed(FakeToolBox):
        def run(self, name: str, tool_input: Mapping[str, Any]) -> ToolOutcome:
            if name == "run_sql":
                return ToolOutcome("{}", False, None)
            return super().run(name, tool_input)

    client = FakeClient(
        [
            _tools(
                _tool_use("t1", tool_input={"text": "leeks", "limit": 5}),
                _tool_use("t2", "run_sql", {"query": "x"}),
            ),
            _tools(_tool_use("t3", tool_input={"text": "barley", "limit": 5})),
            _answer(),
        ]
    )
    _run(client, Mixed(), on_progress=seen.append)
    assert seen == ["Looking for leeks", "Looking for barley"]


def test_sdk_retries_are_off_for_every_call() -> None:
    client = FakeClient([_tools(_tool_use("t1")), _answer()])
    _run(client)
    assert client.options == [{"max_retries": 0}, {"max_retries": 0}]


def test_cancel_checked_before_each_tool_run() -> None:
    cancel = threading.Event()
    toolbox = FakeToolBox(on_run=lambda _n: cancel.set())
    client = FakeClient([_tools(_tool_use("t1"), _tool_use("t2")), _answer()])
    with pytest.raises(AgentCancelled):
        _run(client, toolbox, cancel=cancel)
    assert [r[0] for r in toolbox.runs] == ["keyword_search"]  # t2 never ran
    assert len(client.calls) == 1


def test_deadline_checked_before_each_tool_run() -> None:
    clock = Clock(0.0)

    def advance(_name: str) -> None:
        clock.now = 30.0

    toolbox = FakeToolBox(on_run=advance)
    client = FakeClient([_tools(_tool_use("t1"), _tool_use("t2")), _answer()])
    with pytest.raises(AgentTimeout):
        _run(client, toolbox, clock=clock)
    assert len(toolbox.runs) == 1


def test_no_call_with_too_little_time_left() -> None:
    clock = Clock(0.0)

    def advance(_name: str) -> None:
        clock.now = 28.6  # 1.4 s left: a call could not finish

    client = FakeClient([_tools(_tool_use("t1")), _answer()])
    with pytest.raises(AgentTimeout):
        _run(client, FakeToolBox(on_run=advance), clock=clock)
    assert len(client.calls) == 1


def test_pause_turn_at_the_cap_forces_answer_without_notice() -> None:
    paused = _response("pause_turn", [SimpleNamespace(type="thinking", thinking="", signature="s")])
    client = FakeClient([_tools(_tool_use("t1")), paused, _answer()])
    answer = _run(client, limits=AgentLimits(rounds=2))
    assert answer.kind == "text"
    last = client.calls[2]
    assert last["tool_choice"] == {"type": "none"}
    assert last["messages"][-1]["role"] == "assistant"
    assert all(m["role"] != "system" for m in last["messages"])


def test_limits_are_frozen() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        AgentLimits().rounds = 3  # type: ignore[misc]
