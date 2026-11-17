"""POST /search/find (Cayenne's Brave search turn, 2026-11-17): Brave rows for Add Recipe's search turn.

Every collaborator is a fake: Brave answers from presets (or through ``httpx.MockTransport``), the
reader database is ``FakeDb`` and the gate's Supabase is a ``MagicMock``. No network, no live project.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

from recipeparser.adapters import search_api
from recipeparser.adapters.ai_gate import AiGate
from recipeparser.adapters.ai_search import ai_db
from recipeparser.adapters.ai_search.ai_db import QueryResult
from recipeparser.adapters.brave import BraveError, BraveResult, BraveSearch

MODEL = "claude-opus-5-5"
SUB = "11111111-1111-1111-1111-111111111111"
PAYLOAD = {"sub": SUB, "role": "authenticated", "aud": "authenticated", "email": "cook@example.com"}
OWNED = "aaaaaaaa-0000-0000-0000-000000000001"
AUTH = {"Authorization": "Bearer t"}
WIRE_KEYS = {"url", "title", "site", "note", "status", "recipe_id"}
COOK_SITES = [("seriouseats.com", 9, "Serious Eats"), ("food52.com", 12, "Food52")]

SE_1 = "https://www.seriouseats.com/pad-thai"
SE_2 = "https://www.seriouseats.com/pad-thai-2"
SE_3 = "https://www.seriouseats.com/pad-thai-3"
SE_4 = "https://www.seriouseats.com/pad-thai-4"
F52 = "https://food52.com/recipes/1-pad-thai"
WEB = [f"https://site{i}.example/pad-thai" for i in range(8)]


class FakeDb:
    """Answers the two fixed queries: the cook's sites, and their recipes' source addresses."""

    def __init__(
        self, sites: list[tuple[str, int, str | None]] | None = None, own: list[tuple[str, str]] | None = None
    ) -> None:
        self.sites = sites if sites is not None else list(COOK_SITES)
        self.own = own or []

    def run(
        self, claims: Any, sql: str, params: Any = None, *, max_rows: int = 200, timeout_ms: int | None = None
    ) -> QueryResult:
        if sql == ai_db._COOK_SITES:
            return QueryResult(["host", "recipes", "site"], list(self.sites), False, 1.0)
        if sql == ai_db._OWN_SOURCE_URLS:
            return QueryResult(["id", "source_url"], list(self.own), False, 1.0)
        raise AssertionError(f"unexpected statement: {sql}")

    def close(self) -> None:
        pass


class FakeBrave:
    """Records each query; answers the sites request (it has ``site:``) and the web request from
    their presets: a result list, an exception to raise, or a number of seconds to sleep first."""

    def __init__(self, *, sites: Any = (), web: Any = ()) -> None:
        self.sites = sites
        self.web = web
        self.queries: list[str] = []
        self.counts: list[int] = []
        self._lock = threading.Lock()

    def search(self, query: str, *, count: int = 10) -> list[BraveResult]:
        with self._lock:
            self.queries.append(query)
            self.counts.append(count)
        preset = self.sites if "site:" in query else self.web
        if isinstance(preset, BaseException):
            raise preset
        if isinstance(preset, (int, float)):
            time.sleep(preset)
            return []
        return list(preset)


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
    *,
    db: Any = "default",
    brave: Any = "default",
    gate: AiGate | None = None,
    service: Any = None,
    client: Any = None,
) -> tuple[TestClient, dict[str, Any]]:
    state: dict[str, Any] = {
        "db": FakeDb() if db == "default" else db,
        "brave": FakeBrave(sites=_sites_results(), web=_web_results()) if brave == "default" else brave,
        "gate": gate or AiGate(lambda: _supabase()),
    }
    app = FastAPI()
    app.include_router(
        search_api.build_router(
            _verify,
            state["gate"],
            lambda: state["db"],
            lambda: client,
            MagicMock,
            lambda: service,
            MODEL,
            lambda: state["brave"],
        )
    )
    return TestClient(app, raise_server_exceptions=False), state


def _sites_results() -> list[BraveResult]:
    return [
        BraveResult(SE_1, "Pad Thai | Serious Eats", "Sweet, sour, salty.", profile_name="Serious Eats"),
        BraveResult(SE_2, "Pad Thai Two", has_recipe=True, recipe_title="Pad Thai, Second Try"),
        BraveResult(F52, "Food52 Pad Thai", profile_name="Food52"),
        BraveResult(SE_3, "Pad Thai Three"),
        BraveResult(SE_4, "Pad Thai Four"),
    ]


def _web_results() -> list[BraveResult]:
    return [BraveResult(url, f"Pad Thai {i}", profile_name=f"Site {i}") for i, url in enumerate(WEB)]


def _post(tc: TestClient, body: dict[str, Any] | None = None) -> httpx.Response:
    return tc.post("/search/find", json=body or {"query": "pad thai"}, headers=AUTH)


def _rows(reply: dict[str, Any]) -> list[dict[str, Any]]:
    return [*reply["sites"], *reply["web"]]


# -- the reply ------------------------------------------------------------------------------------


def test_find_returns_sites_then_web() -> None:
    brave = FakeBrave(sites=_sites_results(), web=_web_results())
    db = FakeDb(sites=[("seriouseats.com", 9, "Serious Eats"), ("food52.com", 12, "Food52")])
    tc, _ = _app(db=db, brave=brave)
    resp = _post(tc)
    assert resp.status_code == 200
    reply = resp.json()

    assert len(brave.queries) == 2
    assert brave.counts == [10, 10]
    assert any(q.endswith("(site:seriouseats.com OR site:food52.com)") for q in brave.queries)
    assert "pad thai recipe" in brave.queries

    assert reply["sites_asked"] is True
    assert 0 < len(reply["sites"]) <= 3
    assert 0 < len(reply["web"]) <= 5
    # In the sites group the recipe-block result comes first, then Brave's order.
    assert [r["url"] for r in reply["sites"]] == [SE_2, SE_1, F52]
    se = next(r for r in reply["sites"] if r["url"] == SE_1)
    assert se["site"] == "Serious Eats"
    assert se["title"] == "Pad Thai"
    assert se["note"] == "Sweet, sour, salty."
    assert reply["sites"][0]["title"] == "Pad Thai, Second Try"
    assert [r["url"] for r in reply["web"]] == WEB[:5]
    assert reply["web"][0]["site"] == "Site 0"
    assert all("gap" not in r for r in _rows(reply))
    assert all(r["status"] == "importable" for r in _rows(reply))


def test_find_web_skips_what_sites_kept() -> None:
    web = [
        BraveResult(SE_1 + "?utm=x", "Pad Thai again"),
        BraveResult("https://example.org/excluded/", "Excluded"),
        *_web_results(),
    ]
    tc, _ = _app(brave=FakeBrave(sites=_sites_results(), web=web))
    reply = _post(tc, {"query": "pad thai", "exclude": ["https://www.example.org/excluded"]}).json()
    web_urls = [r["url"] for r in reply["web"]]
    assert SE_1 + "?utm=x" not in web_urls
    assert "https://example.org/excluded/" not in web_urls
    assert web_urls == WEB[:5]


def test_find_without_cook_sites_sends_one_request() -> None:
    brave = FakeBrave(sites=_sites_results(), web=_web_results())
    tc, _ = _app(db=FakeDb(sites=[]), brave=brave)
    reply = _post(tc).json()
    assert brave.queries == ["pad thai recipe"]
    assert reply["sites_asked"] is False
    assert reply["sites"] == []
    assert len(reply["web"]) == 5


def test_find_one_request_failing_leaves_that_group_empty() -> None:
    tc, _ = _app(brave=FakeBrave(sites=BraveError("Brave returned HTTP 500"), web=_web_results()))
    resp = _post(tc)
    assert resp.status_code == 200
    reply = resp.json()
    assert reply["sites"] == []
    assert reply["sites_asked"] is True
    assert len(reply["web"]) == 5


def test_find_a_slow_request_counts_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_api, "FIND_TIMEOUT_S", 0.2)
    tc, _ = _app(brave=FakeBrave(sites=_sites_results(), web=1.0))
    started = time.monotonic()
    reply = _post(tc).json()
    assert time.monotonic() - started < 0.9  # the slow request was not waited for
    assert reply["web"] == []
    assert len(reply["sites"]) == 3


def test_find_every_request_failing_is_502() -> None:
    error = BraveError("Brave returned HTTP 500")
    tc, _ = _app(brave=FakeBrave(sites=error, web=error))
    resp = _post(tc)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Couldn't search the web just now."


def test_find_the_only_request_failing_is_502() -> None:
    tc, _ = _app(db=FakeDb(sites=[]), brave=FakeBrave(web=BraveError("x")))
    resp = _post(tc)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Couldn't search the web just now."


def test_find_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[set[str]] = []

    def fake_blocked(sb: Any, hosts: Any, now: Any) -> set[str]:
        seen.append(set(hosts))
        return {"food52.com"}

    monkeypatch.setattr(search_api.refusing_hosts, "blocked_hosts", fake_blocked)
    db = FakeDb(own=[(OWNED, "https://seriouseats.com/pad-thai/")])
    tc, _ = _app(db=db, service=object())
    reply = _post(tc).json()
    by_url = {r["url"]: r for r in _rows(reply)}
    assert by_url[SE_1]["status"] == "yours"
    assert by_url[SE_1]["recipe_id"] == OWNED
    assert by_url[F52]["status"] == "blocked"
    assert "recipe_id" not in by_url[F52]
    assert by_url[WEB[0]]["status"] == "importable"
    assert len(seen) == 1 and {"seriouseats.com", "food52.com", "site0.example"} <= seen[0]


def test_find_rows_hold_only_wire_fields() -> None:
    secret = "SECRET-INGREDIENT-tamarind-paste"

    def handler(request: httpx.Request) -> httpx.Response:
        item = {
            "url": SE_1 if "site:" in request.url.params["q"] else WEB[0],
            "title": "Pad Thai",
            "description": "Classic.",
            "profile": {"name": "Somewhere", "img": "https://x/img.png"},
            "thumbnail": {"src": "https://x/thumb.png"},
            "extra_snippets": [secret],
            "recipe": {
                "title": "Pad Thai",
                "description": "Classic.",
                "ingredients": [secret],
                "instructions": [{"text": secret}],
                "rating": {"ratingValue": 5},
            },
        }
        return httpx.Response(200, json={"web": {"results": [item]}})

    brave = BraveSearch("k", transport=httpx.MockTransport(handler))
    tc, _ = _app(brave=brave)
    resp = _post(tc)
    assert resp.status_code == 200
    rows = _rows(resp.json())
    assert len(rows) == 2
    for row in rows:
        assert set(row) <= WIRE_KEYS
    assert secret not in resp.text


# -- usage and the gate ---------------------------------------------------------------------------


def _spy_usage(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, list[dict[str, Any]]]]:
    captured: list[tuple[str, str, list[dict[str, Any]]]] = []
    real = search_api.usage.attributed

    @contextmanager
    def spy(user_id: str, action: Any, job_id: Any = None, *, get_client: Any) -> Any:
        with real(user_id, action, job_id, get_client=get_client) as ctx:
            try:
                yield ctx
            finally:  # a 502 leaves the block by an exception
                captured.append((user_id, action, ctx.snapshot()))

    monkeypatch.setattr(search_api.usage, "attributed", spy)
    return captured


def test_find_records_usage_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _spy_usage(monkeypatch)
    tc, _ = _app()
    assert _post(tc).status_code == 200
    assert len(captured) == 1
    user_id, action, rows = captured[0]
    assert (user_id, action) == (SUB, "search")
    assert [(r["step"], r["model"], r["calls"]) for r in rows] == [
        ("find", "brave-web-search", 1),
        ("find", "brave-web-search", 1),
    ]


def test_find_records_one_row_without_cook_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _spy_usage(monkeypatch)
    tc, _ = _app(db=FakeDb(sites=[]))
    assert _post(tc).status_code == 200
    assert [(r["step"], r["model"]) for r in captured[0][2]] == [("find", "brave-web-search")]


def test_find_records_usage_even_when_every_request_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _spy_usage(monkeypatch)
    error = BraveError("x")
    tc, _ = _app(brave=FakeBrave(sites=error, web=error))
    assert _post(tc).status_code == 502
    assert [r["step"] for r in captured[0][2]] == ["find", "find"]


def test_find_spends_a_find_slot_not_a_search_slot() -> None:
    gate = AiGate(lambda: _supabase())
    look_client = MagicMock()
    tc, _ = _app(gate=gate, client=look_client)
    for _ in range(10):
        assert _post(tc).status_code == 200
    assert len(gate._find_hour[SUB]) == 10
    assert len(gate._search_hour[SUB]) == 0
    look_body = {
        "question": "pad thai",
        "answer": {"kind": "none", "text": "None.", "gap": "pad thai"},
        "chosen": 0,
        "measuring": "metric, °C",
    }
    # The look fails inside its stream (the client is a MagicMock), but the gate let it start.
    assert tc.post("/search/look", json=look_body, headers=AUTH).status_code == 200
    assert len(gate._search_hour[SUB]) == 1


def test_find_paused_is_503() -> None:
    brave = FakeBrave(sites=_sites_results(), web=_web_results())
    tc, _ = _app(gate=AiGate(lambda: _supabase(paused=True)), brave=brave)
    assert _post(tc).status_code == 503
    assert brave.queries == []


def test_find_rate_limited_is_429_with_retry_after() -> None:
    gate = AiGate(lambda: _supabase())
    brave = FakeBrave(sites=_sites_results(), web=_web_results())
    tc, _ = _app(gate=gate, brave=brave)
    for _ in range(30):
        assert _post(tc).status_code == 200
    sent = len(brave.queries)
    resp = _post(tc)
    assert resp.status_code == 429
    assert resp.json()["detail"].startswith("You can search again in ")
    assert int(resp.headers["Retry-After"]) >= 1
    assert len(brave.queries) == sent


# -- refusals -------------------------------------------------------------------------------------


def test_find_requires_jwt() -> None:
    tc, _ = _app()
    assert tc.post("/search/find", json={"query": "pad thai"}).status_code == 401


def test_find_without_key_is_503() -> None:
    gate = AiGate(lambda: _supabase())
    tc, _ = _app(brave=None, gate=gate)
    resp = _post(tc)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Searching the web isn't set up."
    assert len(gate._find_hour[SUB]) == 0  # no slot spent


def test_find_without_database_is_503() -> None:
    tc, _ = _app(db=None)
    resp = _post(tc)
    assert resp.status_code == 503
    assert resp.json()["detail"] == search_api.NOT_CONFIGURED_DETAIL


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 121},
        {},
        {"query": "pad thai", "exclude": ["https://a.example/"] * 101},
        {"query": "pad thai", "exclude": ["https://a.example/" + "a" * 2000]},
    ],
)
def test_find_rejects_bad_query(body: dict[str, Any]) -> None:
    brave = FakeBrave(sites=_sites_results(), web=_web_results())
    tc, _ = _app(brave=brave)
    assert tc.post("/search/find", json=body, headers=AUTH).status_code == 422
    assert brave.queries == []


def test_find_strips_the_query() -> None:
    brave = FakeBrave(sites=_sites_results(), web=_web_results())
    tc, _ = _app(db=FakeDb(sites=[]), brave=brave)
    assert _post(tc, {"query": "  pad thai  "}).status_code == 200
    assert brave.queries == ["pad thai recipe"]
    assert search_api.FindRequest.model_validate({"query": " " + "x" * 120 + " "}).query == "x" * 120
