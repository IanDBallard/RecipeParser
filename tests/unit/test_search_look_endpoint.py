"""POST /search/look (Cayenne AI search Stage 5, §7.2): the gate, then checked candidates as NDJSON.

Every collaborator is a fake: no live project, no Anthropic or Postgres call. The look agent's
responses use the R0 spike's block shapes.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

from recipeparser.adapters import search_api
from recipeparser.adapters.ai_gate import PAUSED_DETAIL, AiGate
from recipeparser.adapters.ai_search import ai_db
from recipeparser.adapters.ai_search.ai_db import QueryResult
from recipeparser.adapters.ai_search.look import UNSEARCHABLE

MODEL = "claude-opus-5-5"
SUB = "11111111-1111-1111-1111-111111111111"
PAYLOAD = {"sub": SUB, "role": "authenticated", "aud": "authenticated", "email": "cook@example.com"}
OWNED = "aaaaaaaa-0000-0000-0000-000000000001"
GRAVY = "https://food52.com/recipes/78215-ultimate-vegan-thanksgiving-gravy"
MINE = "https://food52.com/recipes/1-squash"
UNREAD = "https://food52.com/recipes/2-unread"

ANSWER = {
    "kind": "recipes",
    "alternatives": [
        {
            "note": "A vegan dinner.",
            "groups": [
                {"heading": "Main", "recipe_ids": [OWNED]},
                {"heading": "Sides", "recipe_ids": [OWNED]},
            ],
            "gaps": [{"group": 1, "missing": "a vegan side"}, {"group": -1, "missing": "a dessert"}],
        }
    ],
}
BODY = {"question": "A vegan dinner for four", "answer": ANSWER, "chosen": 0, "measuring": "metric, °C", "exclude": []}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("AI_READER_DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # The fakes use real hostnames; the known-refused list is switched back on where a test needs it.
    monkeypatch.setattr(search_api, "KNOWN_UNSEARCHABLE", frozenset())
    UNSEARCHABLE.clear()
    yield
    UNSEARCHABLE.clear()


# -- fakes ----------------------------------------------------------------------------------------


class FakeDb:
    """Answers the two fixed queries: the cook's sites, and their recipes' source addresses."""

    def __init__(
        self, sites: list[tuple[str, int, str | None]] | None = None, own: list[tuple[str, str]] | None = None
    ) -> None:
        self.sites = (
            sites if sites is not None else [("food52.com", 12, "Food52"), ("seriouseats.com", 9, "Serious Eats")]
        )
        self.own = own if own is not None else [(OWNED, MINE + "/")]
        self.claims_seen: list[dict[str, Any]] = []

    def run(
        self, claims: Any, sql: str, params: Any = None, *, max_rows: int = 200, timeout_ms: int | None = None
    ) -> QueryResult:
        self.claims_seen.append(dict(claims))
        if sql == ai_db._COOK_SITES:
            return QueryResult(["host", "recipes", "site"], list(self.sites), False, 1.0)
        if sql == ai_db._OWN_SOURCE_URLS:
            return QueryResult(["id", "source_url"], list(self.own), False, 1.0)
        raise AssertionError(f"unexpected statement: {sql}")

    def close(self) -> None:
        pass


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def _response(stop: str, content: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop, content=content, model=MODEL, usage=_usage())


def _fetch(url: str, tid: str) -> list[SimpleNamespace]:
    caller = {"type": "code_execution_20260120", "tool_id": "srvtoolu_ce"}
    use = SimpleNamespace(type="server_tool_use", id=tid, caller=caller, name="web_fetch", input={"url": url})
    content = SimpleNamespace(type="web_fetch_result", url=url, retrieved_at="2026-11-14T00:00:00+00:00", content=None)
    return [use, SimpleNamespace(type="web_fetch_tool_result", tool_use_id=tid, caller=caller, content=content)]


def _search(query: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="server_tool_use", id="srvtoolu_s", caller=None, name="web_search", input={"query": query}
    )


def _found() -> SimpleNamespace:
    candidates = [
        {"gap": 0, "url": GRAVY, "title": "Ultimate Vegan Gravy", "note": "Vegan · 1 hr · shallots, mushrooms"},
        {"gap": 0, "url": UNREAD, "title": "Never read", "note": "x"},
        {"gap": 1, "url": "https://www.food52.com/recipes/1-squash", "title": "Squash", "note": "Vegan · 40 min"},
    ]
    return _response(
        "end_turn",
        [
            _search("vegan side"),
            *_fetch(GRAVY, "f1"),
            *_fetch(MINE, "f2"),
            SimpleNamespace(type="text", text=json.dumps({"candidates": candidates}), citations=None),
        ],
    )


class FakeAnthropic:
    def __init__(self, script: list[Any], on_call: Any = None) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.on_call = on_call

    def with_options(self, **_: Any) -> Any:
        return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=self._create)))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.on_call:
            self.on_call(len(self.calls))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _refused(*domains: str) -> anthropic.BadRequestError:
    message = (
        f"The following domains are not accessible to our user agent: {list(domains)}. Read more: https://x.invalid"
    )
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    return anthropic.BadRequestError(
        f"Error code: 400 - {body}", response=httpx.Response(400, request=request), body=body
    )


def _supabase(*, paused: bool = False) -> MagicMock:
    sb = MagicMock()

    def table(name: str) -> MagicMock:
        q = MagicMock()
        data = (
            [{"paused": paused}]
            if name == "ai_settings"
            else [{"recipes_processed": 0, "recipe_cap": 10, "exempt": False}]
        )
        q.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = data
        return q

    sb.table.side_effect = table
    return sb


_bearer = HTTPBearer(auto_error=False)


def _verify(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict[str, Any]:
    if credentials is None:
        raise HTTPException(401, detail="Missing bearer token")
    return dict(PAYLOAD)


def _app(
    *, db: Any = "default", client: Any = None, gate: AiGate | None = None, service: Any = None
) -> tuple[TestClient, dict[str, Any]]:
    state: dict[str, Any] = {
        "db": FakeDb() if db == "default" else db,
        "client": client if client is not None else FakeAnthropic([_found()]),
        "gate": gate or AiGate(lambda: _supabase()),
    }
    app = FastAPI()
    app.include_router(
        search_api.build_router(
            _verify, state["gate"], lambda: state["db"], lambda: state["client"], MagicMock, lambda: service, MODEL
        )
    )
    return TestClient(app, raise_server_exceptions=False), state


AUTH = {"Authorization": "Bearer t"}


def _lines(resp: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def _post(tc: TestClient, body: dict[str, Any] | None = None) -> httpx.Response:
    return tc.post("/search/look", json=body or BODY, headers=AUTH)


# -- refusals before the stream -------------------------------------------------------------------


def test_requires_jwt() -> None:
    tc, _ = _app()
    assert tc.post("/search/look", json=BODY).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"answer": {**ANSWER, "alternatives": [{**ANSWER["alternatives"][0], "gaps": []}]}},
        {"answer": {"kind": "text", "text": "Three.", "recipe_ids": []}},
        {"answer": {"kind": "out_of_scope"}},
        {"answer": {"kind": "none", "text": "None of your recipes fit that."}},
        {"chosen": 1},  # no second alternative, so no gaps
    ],
)
def test_look_422_without_gaps(change: dict[str, Any]) -> None:
    gate = AiGate(lambda: _supabase())
    client = FakeAnthropic([])
    tc, _ = _app(client=client, gate=gate)
    assert _post(tc, {**BODY, **change}).status_code == 422
    assert client.calls == [] and len(gate._search_hour[SUB]) == 0  # no slot spent


@pytest.mark.parametrize(
    "change",
    [
        {"question": ""},
        {"question": "x" * 1001},
        {"measuring": ""},
        {"measuring": "x" * 61},
        {"chosen": 4},
        {"chosen": -1},
        {"exclude": [GRAVY] * 101},
        {"exclude": ["https://x.example/" + "a" * 2000]},
    ],
)
def test_validation_422(change: dict[str, Any]) -> None:
    tc, _ = _app(client=FakeAnthropic([]))
    assert _post(tc, {**BODY, **change}).status_code == 422


def test_a_none_answers_gap_is_looked_for() -> None:
    body = {**BODY, "answer": {"kind": "none", "text": "You have no vegan desserts.", "gap": "a vegan dessert"}}
    req = search_api.LookRequest.model_validate(body)
    assert req.gaps() == [(None, "a vegan dessert")]


def test_gaps_carry_their_group_headings() -> None:
    assert search_api.LookRequest.model_validate(BODY).gaps() == [("Sides", "a vegan side"), (None, "a dessert")]


def test_not_configured_503() -> None:
    gate = AiGate(lambda: _supabase())
    tc, _ = _app(db=None, gate=gate)
    resp = _post(tc)
    assert (resp.status_code, resp.json()["detail"]) == (503, "Searching more deeply isn't available right now.")
    assert len(gate._search_hour[SUB]) == 0


def test_paused_503() -> None:
    client = FakeAnthropic([])
    tc, _ = _app(client=client, gate=AiGate(lambda: _supabase(paused=True)))
    resp = _post(tc)
    assert (resp.status_code, resp.json()["detail"]) == (503, PAUSED_DETAIL)
    assert client.calls == []


def test_look_spends_one_search_slot() -> None:
    gate = AiGate(lambda: _supabase())
    tc, _ = _app(gate=gate)
    assert _post(tc).status_code == 200
    assert len(gate._search_hour[SUB]) == 1


def test_looks_share_the_search_rate_limit() -> None:
    client = FakeAnthropic([_found() for _ in range(10)])
    tc, _ = _app(client=client)
    for _ in range(10):
        assert _post(tc).status_code == 200
    resp = _post(tc)
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) > 0
    assert len(client.calls) == 10


# -- the stream -----------------------------------------------------------------------------------


def test_look_streams_progress_then_candidates() -> None:
    tc, state = _app()
    resp = _post(tc, {**BODY, "exclude": ["https://example.org/elsewhere"]})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = _lines(resp)
    assert lines[0] == {"type": "progress", "text": "Looking for vegan side"}
    assert [line["type"] for line in lines] == ["progress", "progress", "progress", "candidates"]
    assert lines[-1] == {
        "type": "candidates",
        "candidates": [
            {
                "gap": 0,
                "url": GRAVY,
                "title": "Ultimate Vegan Gravy",
                "site": "Food52",
                "note": "Vegan · 1 hr · shallots, mushrooms",
                "status": "importable",
            },
            {
                "gap": 1,
                "url": MINE,  # the address the fetch read, not the model's www. spelling
                "title": "Squash",
                "site": "Food52",
                "note": "Vegan · 40 min",
                "status": "yours",
                "recipe_id": OWNED,
            },
        ],
    }
    call = state["client"].calls[0]
    tools = {t["name"]: t for t in call["tools"]}
    assert tools["web_search"]["allowed_domains"] == ["food52.com", "seriouseats.com"]
    user_text = call["messages"][0]["content"]
    assert "0. a vegan side (for Sides)" in user_text and "1. a dessert" in user_text
    assert "https://example.org/elsewhere" in user_text
    assert state["db"].claims_seen and all(c == PAYLOAD for c in state["db"].claims_seen)


def test_scope_defaults_to_sites() -> None:
    tc, state = _app()
    assert _lines(_post(tc))[-1]["type"] == "candidates"
    tools = {t["name"]: t for t in state["client"].calls[0]["tools"]}
    assert tools["web_search"]["allowed_domains"] == ["food52.com", "seriouseats.com"]
    assert "blocked_domains" not in tools["web_search"]


def test_scope_rejects_unknown_value() -> None:
    tc, _ = _app()
    assert _post(tc, {**BODY, "scope": "everywhere"}).status_code == 422


WEB = {**BODY, "scope": "web"}


def test_web_scope_sends_blocked_domains_cook_sites_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_api, "KNOWN_UNSEARCHABLE", frozenset({"nytimes.com"}))
    monkeypatch.setattr(search_api.refusing_hosts, "blocked_now", lambda sb, now: {"x.com"})
    db = FakeDb(sites=[("food52.com", 12, "Food52"), ("smittenkitchen.com", 9, None)])
    tc, state = _app(db=db, service=object())
    assert _lines(_post(tc, WEB))[-1]["type"] == "candidates"
    for tool in state["client"].calls[0]["tools"]:
        assert "allowed_domains" not in tool
        assert tool["blocked_domains"][:2] == ["food52.com", "smittenkitchen.com"]
        assert set(tool["blocked_domains"][2:]) == {"x.com", "nytimes.com"}


def test_web_blocked_trims_at_the_cap_and_keeps_the_cook_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_api, "MAX_BLOCKED_DOMAINS", 3)
    monkeypatch.setattr(search_api, "KNOWN_UNSEARCHABLE", frozenset({"nytimes.com"}))
    monkeypatch.setattr(search_api.refusing_hosts, "blocked_now", lambda sb, now: {"a.com", "b.com", "food52.com"})
    db = FakeDb(sites=[("food52.com", 12, "Food52"), ("smittenkitchen.com", 9, None)])
    blocked, cook = search_api._web_blocked(db, PAYLOAD, lambda: object())
    assert blocked == ["food52.com", "smittenkitchen.com", "a.com"]
    assert cook == {"food52.com": "Food52", "smittenkitchen.com": "smittenkitchen.com"}


def test_web_blocked_survives_a_refusing_hosts_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_api, "KNOWN_UNSEARCHABLE", frozenset({"nytimes.com"}))
    UNSEARCHABLE.add({"epicurious.com"})

    def boom(sb: Any, now: Any) -> set[str]:
        raise RuntimeError("db down")

    monkeypatch.setattr(search_api.refusing_hosts, "blocked_now", boom)
    blocked, _ = search_api._web_blocked(FakeDb(), PAYLOAD, lambda: object())
    assert blocked == ["food52.com", "seriouseats.com", "epicurious.com", "nytimes.com"]


def test_web_scope_unsearchable_sites_fail_and_are_remembered() -> None:
    client = FakeAnthropic([_refused("bbc.co.uk")])
    tc, _ = _app(client=client)
    lines = _lines(_post(tc, WEB))
    assert lines[-1]["type"] == "error" and lines[-1]["code"] == "failed"
    assert "bbc.co.uk" in UNSEARCHABLE.current()
    assert len(client.calls) == 1  # no retry in the web scope


def test_web_scope_never_returns_no_sites() -> None:
    tc, _ = _app(db=FakeDb(sites=[]))
    lines = _lines(_post(tc, WEB))
    assert lines[-1]["type"] == "candidates"
    assert all(line.get("code") != "no_sites" for line in lines)


def test_web_scope_emits_a_find_off_the_cook_sites() -> None:
    url = "https://anysite.example/r/1"
    candidates = [{"gap": 0, "url": url, "title": "Any", "note": "x"}]
    response = _response(
        "end_turn",
        [*_fetch(url, "f1"), SimpleNamespace(type="text", text=json.dumps({"candidates": candidates}), citations=None)],
    )
    tc, _ = _app(client=FakeAnthropic([response]))
    found = _lines(_post(tc, WEB))[-1]["candidates"]
    assert len(found) == 1
    assert found[0]["site"] == "anysite.example" and found[0]["status"] == "importable"


def test_web_scope_uses_the_web_prompt() -> None:
    from recipeparser.adapters.ai_search.look_prompt import web_look_system_blocks

    tc, state = _app()
    _post(tc, WEB)
    assert state["client"].calls[0]["system"] == web_look_system_blocks()


def test_web_scope_spends_one_search_slot() -> None:
    gate = AiGate(lambda: _supabase())
    tc, _ = _app(gate=gate)
    assert _post(tc, WEB).status_code == 200
    assert len(gate._search_hour[SUB]) == 1


def test_blocked_hosts_mark_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[set[str]] = []

    def fake_blocked(sb: Any, hosts: Any, now: Any) -> set[str]:
        seen.append(set(hosts))
        return {"food52.com"}

    monkeypatch.setattr(search_api.refusing_hosts, "blocked_hosts", fake_blocked)
    tc, _ = _app(service=object())
    candidates = _lines(_post(tc))[-1]["candidates"]
    assert [c["status"] for c in candidates] == ["blocked", "yours"]
    assert seen == [{"food52.com"}]


def test_nothing_found_is_an_empty_candidates_line() -> None:
    final = _response("end_turn", [SimpleNamespace(type="text", text='{"candidates": []}', citations=None)])
    tc, _ = _app(client=FakeAnthropic([final]))
    assert _lines(_post(tc)) == [{"type": "candidates", "candidates": []}]


def test_look_no_sites_error_line() -> None:
    client = FakeAnthropic([])
    tc, _ = _app(db=FakeDb(sites=[]), client=client)
    resp = _post(tc)
    assert resp.status_code == 200
    assert _lines(resp) == [
        {"type": "error", "code": "no_sites", "detail": "None of your recipes come from a website."}
    ]
    assert client.calls == []


def test_refused_site_is_dropped_and_the_look_retried() -> None:
    client = FakeAnthropic([_refused("seriouseats.com"), _found()])
    tc, _ = _app(client=client)
    lines = _lines(_post(tc))
    assert lines[-1]["type"] == "candidates" and len(lines[-1]["candidates"]) == 2
    assert [c["tools"][0]["allowed_domains"] for c in client.calls] == [
        ["food52.com", "seriouseats.com"],
        ["food52.com"],
    ]
    assert UNSEARCHABLE.current() == {"seriouseats.com"}
    # The next look skips it from the start.
    client.script.append(_found())
    _post(tc)
    assert client.calls[-1]["tools"][0]["allowed_domains"] == ["food52.com"]


def test_retry_rereads_the_sites_so_the_next_ones_move_up() -> None:
    """Ruling 14: the refused sites are replaced by the cook's next sites, still the top 8."""
    hosts = ["food52.com", "seriouseats.com"] + [f"site{i}.com" for i in range(3, 11)]  # 10 hosts
    db = FakeDb(sites=[(h, 20 - i, None) for i, h in enumerate(hosts)])
    client = FakeAnthropic([_refused("seriouseats.com"), _found()])
    tc, _ = _app(db=db, client=client)
    assert _lines(_post(tc))[-1]["type"] == "candidates"
    first, second = (c["tools"][0]["allowed_domains"] for c in client.calls)
    assert first == hosts[:8]
    assert second == ["food52.com"] + [f"site{i}.com" for i in range(3, 10)]
    assert len(second) == 8 and "site9.com" in second and "seriouseats.com" not in second


def test_no_searchable_site_is_a_no_sites_line() -> None:
    client = FakeAnthropic([_refused("seriouseats.com", "food52.com")])
    tc, _ = _app(client=client)
    assert _lines(_post(tc)) == [
        {"type": "error", "code": "no_sites", "detail": "None of the sites you use can be searched."}
    ]
    assert len(client.calls) == 1
    # Known unsearchable from the start: no call at all.
    _post(tc)
    assert len(client.calls) == 1


def test_a_second_refusal_is_dropped_too() -> None:
    """Bug 5c6f4904 (2026-11-14): the retry's refill brought in epicurious.com, and the look failed."""
    db = FakeDb(sites=[("food52.com", 3, None), ("seriouseats.com", 2, None), ("nytimes.com", 1, None)])
    client = FakeAnthropic([_refused("seriouseats.com"), _refused("nytimes.com"), _found()])
    tc, _ = _app(db=db, client=client)
    assert _lines(_post(tc))[-1]["type"] == "candidates"
    assert [c["tools"][0]["allowed_domains"] for c in client.calls] == [
        ["food52.com", "seriouseats.com", "nytimes.com"],
        ["food52.com", "nytimes.com"],
        ["food52.com"],
    ]


def test_a_refusal_on_every_attempt_fails() -> None:
    db = FakeDb(sites=[("food52.com", 9, None)] + [(f"refuser{i}.com", 8 - i, None) for i in range(5)])
    client = FakeAnthropic([_refused(f"refuser{i}.com") for i in range(4)])
    tc, _ = _app(db=db, client=client)
    assert _lines(_post(tc))[-1] == {
        "type": "error",
        "code": "failed",
        "detail": "Couldn't answer that just now. Try again.",
    }
    assert len(client.calls) == search_api.MAX_SITE_ATTEMPTS == 4


def test_known_refused_sites_are_skipped_from_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from recipeparser.adapters.ai_search.look import KNOWN_UNSEARCHABLE

    monkeypatch.setattr(search_api, "KNOWN_UNSEARCHABLE", KNOWN_UNSEARCHABLE)
    assert {"epicurious.com", "bbcgoodfood.com", "seriouseats.com", "nytimes.com"} <= KNOWN_UNSEARCHABLE
    assert {"thespruceeats.com", "bbc.co.uk"} <= KNOWN_UNSEARCHABLE  # seen refused 2026-11-14
    assert "cooking.nytimes.com" not in KNOWN_UNSEARCHABLE  # Anthropic accepts it
    db = FakeDb(sites=[("food52.com", 3, None), ("epicurious.com", 2, None), ("bbcgoodfood.com", 1, None)])
    client = FakeAnthropic([_found()])
    tc, _ = _app(db=db, client=client)
    assert _lines(_post(tc))[-1]["type"] == "candidates"
    assert [c["tools"][0]["allowed_domains"] for c in client.calls] == [["food52.com"]]


def test_only_known_refused_sites_is_a_no_sites_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from recipeparser.adapters.ai_search.look import KNOWN_UNSEARCHABLE

    monkeypatch.setattr(search_api, "KNOWN_UNSEARCHABLE", KNOWN_UNSEARCHABLE)
    client = FakeAnthropic([])
    tc, _ = _app(db=FakeDb(sites=[("epicurious.com", 2, None)]), client=client)
    assert _lines(_post(tc)) == [
        {"type": "error", "code": "no_sites", "detail": "None of the sites you use can be searched."}
    ]
    assert client.calls == []


def test_timeout_becomes_error_line() -> None:
    tc, _ = _app(client=FakeAnthropic([anthropic.APITimeoutError(httpx.Request("POST", "https://x.invalid"))]))
    assert _lines(_post(tc))[-1] == {
        "type": "error",
        "code": "timeout",
        "detail": "That took too long to answer. Try again.",
    }


def test_failure_becomes_error_line(caplog: pytest.LogCaptureFixture) -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    error = anthropic.APIStatusError("boom", response=httpx.Response(500, request=request), body=None)
    tc, _ = _app(client=FakeAnthropic([error]))
    with caplog.at_level("ERROR"):
        lines = _lines(_post(tc))
    assert lines == [{"type": "error", "code": "failed", "detail": "Couldn't answer that just now. Try again."}]


def test_usage_attributed_to_search_as_step_look(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, list[str]]] = []
    real = search_api.usage.attributed

    from contextlib import contextmanager

    @contextmanager
    def spy(user_id: str, action: Any, job_id: Any = None, *, get_client: Any) -> Any:
        with real(user_id, action, job_id, get_client=get_client) as ctx:
            yield ctx
            captured.append((user_id, action, [r["step"] for r in ctx.records]))

    monkeypatch.setattr(search_api.usage, "attributed", spy)
    tc, _ = _app()
    _post(tc)
    assert captured == [(SUB, "search", ["look"])]


def test_look_cancel_stops_before_next_call() -> None:
    """The cook leaves after the first call: the look stops before its next one and says nothing more."""
    cancel = threading.Event()
    paused = _response("pause_turn", [_search("vegan side")])
    client = FakeAnthropic([paused, _found()], on_call=lambda n: cancel.set())

    async def drive() -> list[bytes]:
        return [
            line
            async for line in search_api.look_stream(
                body=search_api.LookRequest.model_validate(BODY),
                claims=dict(PAYLOAD),
                db=FakeDb(),  # type: ignore[arg-type]
                client=client,  # type: ignore[arg-type]
                get_service_client=lambda: None,
                model=MODEL,
                cancel=cancel,
            )
        ]

    lines = [json.loads(b) for b in asyncio.run(drive())]
    assert lines == [{"type": "progress", "text": "Looking for vegan side"}]
    assert len(client.calls) == 1
