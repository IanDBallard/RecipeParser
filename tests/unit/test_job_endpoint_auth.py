"""The four job endpoints must verify the token and the owner (spec 4.6)."""
from __future__ import annotations

import pytest
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
def client(monkeypatch):
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
