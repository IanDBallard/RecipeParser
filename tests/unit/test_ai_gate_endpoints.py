# tests/unit/test_ai_gate_endpoints.py
"""Every AI endpoint is gated and attributed; nothing else is gated."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from recipeparser.adapters import ai_gate, api


@pytest.fixture()
def client(monkeypatch):
    api.app.dependency_overrides[api._verify_supabase_jwt] = lambda: {"sub": "u1"}
    yield TestClient(api.app, raise_server_exceptions=False)
    api.app.dependency_overrides.clear()


def _refuse(code, detail):
    def check(user_id, *, imports=False, embed=False):
        raise HTTPException(code, detail=detail)
    return check


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/embed", {"text": "cake"}),
        ("post", "/jobs", {"text": "Boil water."}),
        ("post", "/jobs/recategorize", {"category_ids": ["c1"]}),
        ("post", "/recipes/r1/image/generate", {"title": "Cake"}),
        ("post", "/shopping/classify", {"ingredients": [{"key": "k", "text": "1 egg"}], "known_foods": []}),
    ],
)
def test_paused_refuses_every_ai_endpoint_with_503(client, monkeypatch, method, path, body) -> None:
    monkeypatch.setattr(api._ai_gate, "check", _refuse(503, ai_gate.PAUSED_DETAIL))
    resp = getattr(client, method)(path, json=body)
    assert (resp.status_code, resp.json()["detail"]) == (503, ai_gate.PAUSED_DETAIL)


def test_file_jobs_are_gated_as_imports(client, monkeypatch) -> None:
    seen = {}

    def check(user_id, *, imports=False, embed=False):
        seen.update(imports=imports, embed=embed)
        raise HTTPException(429, detail="You've used all 5 of your beta recipe imports.")

    monkeypatch.setattr(api._ai_gate, "check", check)
    resp = client.post("/jobs/file", files={"file": ("a.txt", b"x", "text/plain")})
    assert resp.status_code == 429
    assert seen == {"imports": True, "embed": False}


def test_embed_is_gated_with_the_rate_limit(client, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(api._ai_gate, "check", lambda user_id, *, imports=False, embed=False: seen.update(imports=imports, embed=embed))
    with patch("recipeparser.adapters.api._get_client", return_value=MagicMock()), \
         patch("recipeparser.gemini.get_embeddings", return_value=[0.0] * 1536):
        assert client.post("/embed", json={"text": "cake"}).status_code == 200
    assert seen == {"imports": False, "embed": True}


def test_status_and_cancel_are_not_gated(client, monkeypatch) -> None:
    monkeypatch.setattr(api._ai_gate, "check", _refuse(503, ai_gate.PAUSED_DETAIL))
    assert client.get("/jobs/nope").status_code != 503
    assert client.post("/jobs/nope/cancel").status_code != 503


def test_embed_records_under_search(client, monkeypatch) -> None:
    monkeypatch.setattr(api._ai_gate, "check", lambda *a, **k: None)
    captured = []
    real = api.usage.attributed

    def spy(user_id, action, job_id=None, *, get_client):
        captured.append((user_id, action))
        return real(user_id, action, job_id, get_client=get_client)

    monkeypatch.setattr(api.usage, "attributed", spy)
    with patch("recipeparser.adapters.api._get_client", return_value=MagicMock()), \
         patch("recipeparser.gemini.get_embeddings", return_value=[0.0] * 1536):
        client.post("/embed", json={"text": "cake"})
    assert captured == [("u1", "search")]
