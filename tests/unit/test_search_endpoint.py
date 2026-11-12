"""POST /search/ask (Cayenne AI search §7.1): the gate, then the agent's answer as an NDJSON stream.

Every collaborator is a fake: no live project, no Anthropic, Gemini or Postgres call.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

from recipeparser.adapters import api, search_api
from recipeparser.adapters.ai_gate import PAUSED_DETAIL, AiGate
from recipeparser.adapters.ai_search import agent as agent_mod
from recipeparser.adapters.ai_search.ai_db import Catalogue, ColumnInfo, QueryResult, ViewInfo

MODEL = "claude-opus-5-5"
SUB = "11111111-1111-1111-1111-111111111111"
PAYLOAD = {"sub": SUB, "role": "authenticated", "aud": "authenticated", "email": "cook@example.com"}
OWNED = str(uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001"))
OWNED_2 = str(uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002"))
INVENTED = str(uuid.UUID("bbbbbbbb-0000-0000-0000-000000000009"))
BODY = {"text": "a quick soup", "today": "2026-11-11", "measuring": "metric, °C", "filters": []}


@pytest.fixture(autouse=True)
def _no_live_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_READER_DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# -- fakes ----------------------------------------------------------------------------------------


class FakeDb:
    def __init__(self, owned: set[str] | None = None) -> None:
        self.owned = owned if owned is not None else {OWNED, OWNED_2}
        self.claims_seen: list[dict[str, Any]] = []
        self.timeouts: list[int | None] = []

    def catalogue(self) -> Catalogue:
        return Catalogue(views={"recipes": ViewInfo("", [ColumnInfo("id", "uuid", "")])}, functions={})

    def run(
        self, claims: Any, sql: str, params: Any = None, *, max_rows: int = 200, timeout_ms: int | None = None
    ) -> QueryResult:
        self.claims_seen.append(dict(claims))
        self.timeouts.append(timeout_ms)
        return QueryResult(["id", "title", "rank"], [(OWNED, "Leek soup", 1.0)], False, 1.0)

    def own_recipe_ids(self, claims: Any, ids: Any) -> set[str]:
        self.claims_seen.append(dict(claims))
        return {i for i in ids if i in self.owned}

    def close(self) -> None:
        pass


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def _response(stop: str, content: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop, content=content, model=MODEL, usage=_usage())


def _tool_round(text: str = "soup") -> SimpleNamespace:
    return _response(
        "tool_use",
        [SimpleNamespace(type="tool_use", id="t1", name="keyword_search", input={"text": text, "limit": 5})],
    )


def _final(payload: dict[str, Any]) -> SimpleNamespace:
    return _response("end_turn", [SimpleNamespace(type="text", text=json.dumps(payload))])


def _recipes(*ids: str) -> dict[str, Any]:
    return {
        "kind": "recipes",
        "text": "",
        "recipe_ids": [],
        "alternatives": [{"label": "", "note": "Quick ones.", "groups": [{"heading": "", "recipe_ids": list(ids)}]}],
    }


class FakeAnthropic:
    def __init__(self, script: list[Any], on_call: Any = None) -> None:
        self.script = list(script)
        self.calls = 0
        self.on_call = on_call

    def with_options(self, **_: Any) -> Any:
        return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=self._create)))

    def _create(self, **_: Any) -> Any:
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _supabase(*, paused: bool = False, exempt: bool = False) -> MagicMock:
    sb = MagicMock()

    def table(name: str) -> MagicMock:
        q = MagicMock()
        data = [{"paused": paused}] if name == "ai_settings" else [
            {"recipes_processed": 0, "recipe_cap": 10, "exempt": exempt}
        ]
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
    *,
    db: Any = "default",
    client: Any = None,
    gate: AiGate | None = None,
    embed_client: Any = None,
) -> tuple[TestClient, dict[str, Any]]:
    state: dict[str, Any] = {
        "db": FakeDb() if db == "default" else db,
        "client": client if client is not None else FakeAnthropic([_tool_round(), _final(_recipes(OWNED))]),
        "gate": gate or AiGate(lambda: _supabase()),
    }
    app = FastAPI()
    app.include_router(
        search_api.build_router(
            _verify,
            state["gate"],
            lambda: state["db"],
            lambda: state["client"],
            lambda: embed_client or MagicMock(),
            lambda: None,
            MODEL,
        )
    )
    return TestClient(app, raise_server_exceptions=False), state


AUTH = {"Authorization": "Bearer t"}


def _lines(resp: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


# -- refusals before the stream -------------------------------------------------------------------


def test_requires_jwt() -> None:
    tc, _ = _app()
    assert tc.post("/search/ask", json=BODY).status_code == 401


def test_not_configured_503() -> None:
    gate = AiGate(lambda: _supabase())
    tc, _ = _app(db=None, gate=gate)
    resp = tc.post("/search/ask", json=BODY, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (503, "Searching more deeply isn't available right now.")
    assert gate.search_retry_after(SUB) == 0 and len(gate._search_hour[SUB]) == 0  # no slot spent


def test_rate_limited_429_before_stream() -> None:
    script = [_final({"kind": "out_of_scope", "text": "", "recipe_ids": [], "alternatives": []}) for _ in range(10)]
    client = FakeAnthropic(script)
    tc, _ = _app(client=client)
    for _ in range(10):
        assert tc.post("/search/ask", json=BODY, headers=AUTH).status_code == 200
    calls_before = client.calls
    resp = tc.post("/search/ask", json=BODY, headers=AUTH)
    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"].startswith("You can search more deeply again in ")
    assert int(resp.headers["Retry-After"]) > 0
    assert client.calls == calls_before


def test_paused_503() -> None:
    client = FakeAnthropic([])
    tc, _ = _app(client=client, gate=AiGate(lambda: _supabase(paused=True)))
    resp = tc.post("/search/ask", json=BODY, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (503, PAUSED_DETAIL)
    assert client.calls == 0


@pytest.mark.parametrize(
    "change",
    [
        {"text": ""},
        {"text": "x" * 1001},
        {"filters": ["f"] * 13},
        {"filters": ["x" * 201]},
        {"measuring": ""},
        {"today": "not a date"},
        {"previous": {"question": "soup", "answer": {"kind": "out_of_scope"}, "round": 6}},
        {"previous": {"question": "", "answer": {"kind": "out_of_scope"}, "round": 1}},
    ],
)
def test_validation_422(change: dict[str, Any]) -> None:
    client = FakeAnthropic([])
    tc, _ = _app(client=client)
    assert tc.post("/search/ask", json={**BODY, **change}, headers=AUTH).status_code == 422
    assert client.calls == 0


def test_oversize_previous_answer_422() -> None:
    """A 2 MB previous answer would be re-sent, and billed, on every call: it is refused up front."""
    client = FakeAnthropic([])
    gate = AiGate(lambda: _supabase())
    tc, _ = _app(client=client, gate=gate)
    previous = {"question": "soups", "answer": {"kind": "text", "text": "x" * 2_000_000, "recipe_ids": []}, "round": 1}
    resp = tc.post("/search/ask", json={**BODY, "previous": previous}, headers=AUTH)
    assert resp.status_code == 422
    assert client.calls == 0
    assert len(gate._search_hour[SUB]) == 0  # no slot spent


# -- the stream -----------------------------------------------------------------------------------


def test_streams_progress_then_answer() -> None:
    tc, _ = _app()
    resp = tc.post("/search/ask", json=BODY, headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = _lines(resp)
    assert lines[0] == {"type": "progress", "text": "Looking for soup"}
    assert lines[-1] == {
        "type": "answer",
        "answer": {"kind": "recipes", "alternatives": [{"note": "Quick ones.", "groups": [
            {"heading": None, "recipe_ids": [OWNED]}
        ]}]},
    }
    assert [line["type"] for line in lines].count("answer") == 1


def test_answer_ids_limited_to_tool_results() -> None:
    # OWNED_2 is the cook's but no tool returned it; INVENTED is nobody's.
    client = FakeAnthropic([_tool_round(), _final(_recipes(OWNED, OWNED_2, INVENTED))])
    tc, _ = _app(client=client)
    answer = _lines(tc.post("/search/ask", json=BODY, headers=AUTH))[-1]["answer"]
    assert answer["alternatives"][0]["groups"][0]["recipe_ids"] == [OWNED]


def test_previous_answer_ids_are_admitted() -> None:
    previous = {
        "question": "soups",
        "answer": {"kind": "text", "text": "This one.", "recipe_ids": [OWNED_2, INVENTED]},
        "round": 1,
    }
    client = FakeAnthropic([_final(_recipes(OWNED_2, INVENTED))])
    tc, _ = _app(client=client)
    answer = _lines(tc.post("/search/ask", json={**BODY, "previous": previous}, headers=AUTH))[-1]["answer"]
    assert answer["alternatives"][0]["groups"][0]["recipe_ids"] == [OWNED_2]


def test_failure_becomes_error_line(caplog: pytest.LogCaptureFixture) -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    error = anthropic.APIStatusError("boom", response=httpx.Response(500, request=request), body=None)
    tc, _ = _app(client=FakeAnthropic([_tool_round(), error]))
    with caplog.at_level("ERROR"):
        resp = tc.post("/search/ask", json=BODY, headers=AUTH)
    assert resp.status_code == 200
    lines = _lines(resp)
    assert lines[-1] == {"type": "error", "code": "failed", "detail": "Couldn't answer that just now. Try again."}
    assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)


@pytest.mark.parametrize("error_name", ["PoolTimeout", "NotAiReader", "QueryFailed"])
def test_infrastructure_errors_become_failed(error_name: str) -> None:
    from psycopg_pool import PoolTimeout

    from recipeparser.adapters.ai_search.ai_db import NotAiReader, QueryFailed

    errors = {"PoolTimeout": PoolTimeout("t"), "NotAiReader": NotAiReader("n"), "QueryFailed": QueryFailed("q")}

    class BrokenDb(FakeDb):
        def catalogue(self) -> Catalogue:
            raise errors[error_name]

    tc, _ = _app(db=BrokenDb())
    lines = _lines(tc.post("/search/ask", json=BODY, headers=AUTH))
    assert lines == [{"type": "error", "code": "failed", "detail": "Couldn't answer that just now. Try again."}]


def test_timeout_becomes_error_line() -> None:
    tc, _ = _app(client=FakeAnthropic([anthropic.APITimeoutError(httpx.Request("POST", "https://x.invalid"))]))
    lines = _lines(tc.post("/search/ask", json=BODY, headers=AUTH))
    assert lines[-1] == {"type": "error", "code": "timeout", "detail": "That took too long to answer. Try again."}


def test_out_of_scope_passthrough() -> None:
    client = FakeAnthropic([_final({"kind": "out_of_scope", "text": "nope", "recipe_ids": [], "alternatives": []})])
    tc, _ = _app(client=client)
    assert _lines(tc.post("/search/ask", json=BODY, headers=AUTH)) == [
        {"type": "answer", "answer": {"kind": "out_of_scope"}}
    ]


def test_claims_are_the_verified_payload() -> None:
    tc, state = _app()
    tc.post("/search/ask", json=BODY, headers=AUTH)
    assert state["db"].claims_seen and all(c == PAYLOAD for c in state["db"].claims_seen)


def test_usage_attributed_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, int]] = []
    real = search_api.usage.attributed

    from contextlib import contextmanager

    @contextmanager
    def spy(user_id: str, action: Any, job_id: Any = None, *, get_client: Any) -> Any:
        with real(user_id, action, job_id, get_client=get_client) as ctx:
            yield ctx
            captured.append((user_id, action, len(ctx.records)))

    monkeypatch.setattr(search_api.usage, "attributed", spy)
    tc, _ = _app()
    tc.post("/search/ask", json=BODY, headers=AUTH)
    assert captured == [(SUB, "search", 2)]  # two model calls recorded


def test_disconnect_sets_cancel() -> None:
    """Closing the stream early sets the cancel event: the agent stops before its next call."""
    started, release = threading.Event(), threading.Event()

    class SlowDb(FakeDb):
        def run(
            self, claims: Any, sql: str, params: Any = None, *, max_rows: int = 200, timeout_ms: int | None = None
        ) -> QueryResult:
            started.set()
            release.wait(5)
            return super().run(claims, sql, params, max_rows=max_rows, timeout_ms=timeout_ms)

    gate = AiGate(lambda: _supabase())
    gate.check(SUB, search=True)  # the slot the endpoint spends before the stream starts
    client = FakeAnthropic([_tool_round(), _final(_recipes(OWNED))])
    cancel = threading.Event()
    stream = search_api.ask_stream(
        body=search_api.AskRequest.model_validate(BODY),
        claims=dict(PAYLOAD),
        db=SlowDb(),  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        get_gemini=MagicMock,
        get_service_client=lambda: None,
        model=MODEL,
        cancel=cancel,
    )

    async def drive() -> None:
        task = asyncio.ensure_future(stream.__anext__())
        await asyncio.to_thread(started.wait, 5)
        assert not cancel.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()

    asyncio.run(drive())
    assert cancel.is_set()
    release.set()
    for t in threading.enumerate():
        if t.name == search_api.WORKER_NAME:
            t.join(5)
            assert not t.is_alive()
    assert client.calls == 1  # stopped before its next model call
    assert len(gate._search_hour[SUB]) == 1  # the slot stays spent


# -- the real app ---------------------------------------------------------------------------------


def test_no_live_project_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the secrets unset the real app builds neither client and refuses with 503, spending nothing."""
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: None)
    built: list[str] = []
    monkeypatch.setattr(api, "_build_ai_reader_db", lambda dsn: built.append("db"))
    monkeypatch.setattr(api, "_build_anthropic", lambda key: built.append("anthropic"))
    api.app.dependency_overrides[api._verify_supabase_jwt] = lambda: dict(PAYLOAD)
    try:
        resp = TestClient(api.app, raise_server_exceptions=False).post("/search/ask", json=BODY)
    finally:
        api.app.dependency_overrides.clear()
    assert (resp.status_code, resp.json()["detail"]) == (503, "Searching more deeply isn't available right now.")
    assert built == []


def test_real_app_refuses_secrets_in_a_test_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with both secrets set, a test run never builds a connection to a real project."""
    monkeypatch.setenv("AI_READER_DATABASE_URL", "postgresql://ai_reader:x@example.invalid/db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("ALLOW_LIVE_WRITES_IN_TESTS", raising=False)
    built: list[str] = []
    monkeypatch.setattr(api, "_build_ai_reader_db", lambda dsn: built.append("db"))
    monkeypatch.setattr(api, "_build_anthropic", lambda key: built.append("anthropic"))
    assert api._search_db() is None and api._search_anthropic() is None
    assert built == []


def test_real_app_builds_once_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_READER_DATABASE_URL", "postgresql://ai_reader:x@example.invalid/db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)
    monkeypatch.setattr(api, "_search_clients", {})
    built: list[str] = []
    monkeypatch.setattr(api, "_build_ai_reader_db", lambda dsn: built.append(dsn) or FakeDb())
    monkeypatch.setattr(api, "_build_anthropic", lambda key: built.append("anthropic") or object())
    first = (api._search_db(), api._search_anthropic())
    second = (api._search_db(), api._search_anthropic())
    assert first == second and sorted(built) == ["anthropic", "postgresql://ai_reader:x@example.invalid/db"]


def test_agent_model_is_configured() -> None:
    from recipeparser import config

    assert config.AI_SEARCH_MODEL == MODEL
    assert agent_mod.run_agent is search_api.run_agent


def test_embed_is_bounded_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The meaning search's embed call gets at most 8 s of the request's time and no retry."""
    from recipeparser import gemini

    seen: list[dict[str, Any]] = []

    def fake_embeddings(text: str, client: Any, **kwargs: Any) -> list[float]:
        seen.append(kwargs)
        return [0.1, 0.2]

    monkeypatch.setattr(gemini, "get_embeddings", fake_embeddings)
    semantic = _response(
        "tool_use",
        [SimpleNamespace(type="tool_use", id="t1", name="semantic_search", input={"text": "soup", "limit": 5})],
    )
    tc, state = _app(client=FakeAnthropic([semantic, _final(_recipes(OWNED))]))
    lines = _lines(tc.post("/search/ask", json=BODY, headers=AUTH))
    assert lines[-1]["type"] == "answer"
    assert len(seen) == 1
    assert seen[0]["max_retries"] == 0
    assert 0 < seen[0]["timeout_secs"] <= 8.0
    # The search statement's timeout is the request's time left, which ai_db caps at 5 s.
    assert state["db"].timeouts[0] is not None and 0 < state["db"].timeouts[0] <= 30_000
