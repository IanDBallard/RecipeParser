"""The four job endpoints must verify the token and the owner (spec 4.6)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

import recipeparser.adapters.api as api
from recipeparser.core.fsm import PipelineController

OWNER = "11111111-1111-1111-1111-111111111111"
STRANGER = "22222222-2222-2222-2222-222222222222"

PATHS = [
    ("get", "/jobs/job-1"),
    ("post", "/jobs/job-1/pause"),
    ("post", "/jobs/job-1/resume"),
    ("post", "/jobs/job-1/cancel"),
]


@pytest.fixture
def client():
    api._active_jobs.clear()
    api._active_jobs["job-1"] = (OWNER, PipelineController())
    yield TestClient(api.app)
    api._active_jobs.clear()


def _as(user_id: str):
    return lambda: {"sub": user_id}


@pytest.mark.parametrize("method,path", PATHS)
def test_no_token_is_rejected(client, method, path, monkeypatch):
    monkeypatch.setattr(api, "_DISABLE_AUTH", False)
    api.app.dependency_overrides.clear()

    response = getattr(client, method)(path)

    assert response.status_code == 401


@pytest.mark.parametrize("method,path", PATHS)
def test_a_stranger_gets_404_not_403(client, method, path):
    """403 would confirm the job exists, turning these into an id oracle."""
    api.app.dependency_overrides[api._verify_supabase_jwt] = _as(STRANGER)
    try:
        response = getattr(client, method)(path)
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 404


@pytest.mark.parametrize("method,path", PATHS)
def test_the_owner_is_served(client, method, path):
    api.app.dependency_overrides[api._verify_supabase_jwt] = _as(OWNER)
    try:
        response = getattr(client, method)(path)
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# A verified token with no (or an empty) subject must not authenticate at all.
#
# Every write in this service is attributed to `sub`, and _owned_controller's
# ownership check is a plain equality against it. A token that clears
# signature verification but carries no subject would create — and then let a
# second sub-less token reach — a job owned by "". The fix lives in
# _verify_supabase_jwt itself, not in _owned_controller, so /jobs and
# /jobs/file are covered too, not just the four control endpoints.
# ---------------------------------------------------------------------------

def _stub_jwt_decode_to_return(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    """Make the real-token branch of _verify_supabase_jwt return *payload*.

    Patches the actual `jwt` module (PyJWT) that _verify_supabase_jwt imports
    locally as `import jwt as pyjwt`, so no real network call to a JWKS
    endpoint happens.
    """
    import jwt as pyjwt

    monkeypatch.setattr(api, "_DISABLE_AUTH", False)
    mock_jwks_client = MagicMock()
    mock_jwks_client.get_signing_key_from_jwt.return_value.key = "irrelevant-for-the-stub"
    monkeypatch.setattr(pyjwt, "PyJWKClient", lambda url: mock_jwks_client)
    monkeypatch.setattr(pyjwt, "decode", lambda *args, **kwargs: payload)


@pytest.mark.parametrize(
    "payload",
    [{}, {"sub": ""}, {"sub": None}, {"sub": "   "}],
    ids=["missing", "empty", "null", "whitespace"],
)
def test_a_verified_token_without_a_subject_is_rejected(monkeypatch, payload):
    _stub_jwt_decode_to_return(monkeypatch, payload)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="a-token")

    with pytest.raises(HTTPException) as exc_info:
        api._verify_supabase_jwt(credentials=credentials)

    assert exc_info.value.status_code == 401


def test_a_verified_token_with_a_real_subject_is_accepted(monkeypatch):
    _stub_jwt_decode_to_return(monkeypatch, {"sub": OWNER})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="a-token")

    result = api._verify_supabase_jwt(credentials=credentials)

    assert result == {"sub": OWNER}


def test_disable_auth_bypass_still_returns_the_test_subject(monkeypatch):
    """The bypass's subject is validated as a non-empty UUID at startup — the
    new check must not reject it."""
    monkeypatch.setattr(api, "_DISABLE_AUTH", True)
    monkeypatch.setattr(api, "_TEST_USER_ID", "33333333-3333-3333-3333-333333333333")

    result = api._verify_supabase_jwt(credentials=None)

    assert result == {"sub": "33333333-3333-3333-3333-333333333333"}


def test_an_empty_owner_string_still_refuses_a_real_caller(client):
    """An entry that somehow ended up owned by "" (a pre-fix leftover, a bad
    migration, ...) must still be unreachable by an ordinary caller with a
    real, non-empty subject — plain UUID inequality, no special-casing needed
    here."""
    api._active_jobs["orphan"] = ("", PipelineController())
    api.app.dependency_overrides[api._verify_supabase_jwt] = _as(OWNER)
    try:
        response = client.get("/jobs/orphan")
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 404


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_caller_subject_cannot_match_a_blank_owner(client, blank):
    """_owned_controller's second line of defence: a blank subject must never
    match anything, including a registry entry whose stored owner is the same
    blank value. Without the guard, `blank != blank` is False and
    _owned_controller would hand the caller the job — this is the exact trap
    _verify_supabase_jwt's guard exists to make unreachable in practice,
    checked here independently at the ownership layer. Owner and caller use
    the identical blank string so the case actually exercises the equality
    trap rather than an ordinary mismatch.
    """
    api._active_jobs["orphan"] = (blank, PipelineController())
    api.app.dependency_overrides[api._verify_supabase_jwt] = _as(blank)
    try:
        response = client.get("/jobs/orphan")
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 404
