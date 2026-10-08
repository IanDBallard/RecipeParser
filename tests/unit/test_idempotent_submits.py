"""POST /jobs, /jobs/file and /shares honour Idempotency-Key (Cayenne Fix Roadmap F-200). No live writes."""
from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import recipeparser.adapters.api as api
from tests.unit.share_fakes import FakeSupabase

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
FRIEND = "33333333-3333-3333-3333-333333333333"
R1 = "44444444-4444-4444-4444-444444444444"
KEY = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.fixture
def jobs(monkeypatch) -> List[str]:
    """Records each job row the endpoint asks for; nothing runs and nothing is written."""
    made: List[str] = []
    monkeypatch.setattr(api, "_create_ingestion_job", lambda job_id, user_id, source_hint=None: made.append(job_id))

    async def _no_run(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(api, "_run_ingestion_job", _no_run)
    api._idempotency.reset()
    api._active_jobs.clear()
    yield made
    api._active_jobs.clear()


@pytest.fixture
def client():
    yield TestClient(api.app, raise_server_exceptions=False)
    api.app.dependency_overrides.clear()


def _as(client: TestClient, user_id: str) -> TestClient:
    claims: Dict[str, Any] = {"sub": user_id, "email": f"{user_id[:4]}@example.com"}
    api.app.dependency_overrides[api._verify_supabase_jwt] = lambda: claims
    return client


def test_a_repeated_text_job_answers_the_first_job_and_makes_one(client, jobs):
    c = _as(client, USER)
    first = c.post("/jobs", json={"text": "Boil water."}, headers={"Idempotency-Key": KEY})
    again = c.post("/jobs", json={"text": "Boil water."}, headers={"Idempotency-Key": KEY})
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json()
    assert jobs == [first.json()["job_id"]]


def test_without_a_key_every_post_is_a_new_job(client, jobs):
    c = _as(client, USER)
    c.post("/jobs", json={"text": "Boil water."})
    c.post("/jobs", json={"text": "Boil water."})
    assert len(jobs) == 2


def test_another_user_with_the_same_key_gets_their_own_job(client, jobs):
    first = _as(client, USER).post("/jobs", json={"text": "a"}, headers={"Idempotency-Key": KEY})
    other = _as(client, OTHER).post("/jobs", json={"text": "a"}, headers={"Idempotency-Key": KEY})
    assert first.json() != other.json()
    assert len(jobs) == 2


def test_a_400_does_not_spend_the_key(client, jobs):
    c = _as(client, USER)
    assert c.post("/jobs", json={}, headers={"Idempotency-Key": KEY}).status_code == 400
    assert c.post("/jobs", json={"text": "a"}, headers={"Idempotency-Key": KEY}).status_code == 202
    assert len(jobs) == 1


def test_a_malformed_key_is_422_and_makes_nothing(client, jobs):
    resp = _as(client, USER).post("/jobs", json={"text": "a"}, headers={"Idempotency-Key": "bad key"})
    assert resp.status_code == 422
    assert jobs == []


def test_a_repeat_while_the_first_is_still_running_is_409(client, jobs, monkeypatch):
    """Review Focus 1: the second request arrives while the first holds its claim."""
    entered, release = threading.Event(), threading.Event()

    def _slow_create(job_id, user_id, source_hint=None):
        entered.set()
        release.wait(5)
        jobs.append(job_id)

    monkeypatch.setattr(api, "_create_ingestion_job", _slow_create)
    c = _as(client, USER)
    answers: Dict[str, Any] = {}
    first = threading.Thread(target=lambda: answers.setdefault(
        "first", c.post("/jobs", json={"text": "a"}, headers={"Idempotency-Key": KEY})))
    first.start()
    assert entered.wait(5)
    second = c.post("/jobs", json={"text": "a"}, headers={"Idempotency-Key": KEY})
    release.set()
    first.join(5)
    assert second.status_code == 409
    assert second.json()["detail"] == "That request is still being handled. Wait a moment and look again."
    assert answers["first"].status_code == 202
    assert len(jobs) == 1


def test_a_repeated_file_job_answers_the_first_job(client, jobs):
    c = _as(client, USER)
    files = {"file": ("r.epub", b"PK\x03\x04epub", "application/epub+zip")}
    first = c.post("/jobs/file", files=files, headers={"Idempotency-Key": KEY})
    again = c.post("/jobs/file", files=files, headers={"Idempotency-Key": KEY})
    assert first.status_code == 202
    assert again.json() == first.json()
    assert len(jobs) == 1


def test_a_replayed_file_job_writes_no_temp_file(client, jobs, monkeypatch):
    """Review Focus 3: a retried Paprika library is answered before it is copied anywhere."""
    c = _as(client, USER)
    files = {"file": ("lib.paprikarecipes", b"PK\x03\x04", "application/octet-stream")}
    c.post("/jobs/file", files=files, headers={"Idempotency-Key": KEY})

    def _boom(*_a, **_k):
        raise AssertionError("a replay must not write a temp file")

    monkeypatch.setattr(api.tempfile, "NamedTemporaryFile", _boom)
    again = c.post("/jobs/file", files=files, headers={"Idempotency-Key": KEY})
    assert again.status_code == 202


def test_a_413_does_not_spend_the_key(client, jobs, monkeypatch):
    """Review Focus 2: the cook replaces an oversized file and sends again."""
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 4)
    c = _as(client, USER)
    big = {"file": ("r.pdf", b"%PDF-1.4 long enough", "application/pdf")}
    assert c.post("/jobs/file", files=big, headers={"Idempotency-Key": KEY}).status_code == 413
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 10_000)
    assert c.post("/jobs/file", files=big, headers={"Idempotency-Key": KEY}).status_code == 202
    assert len(jobs) == 1


@pytest.fixture
def share_fake(monkeypatch) -> FakeSupabase:
    f = FakeSupabase()
    f.rpcs["user_id_for_email"] = lambda p: FRIEND if p["p_email"] == "friend@example.com" else None
    f.rpcs["create_recipe_share"] = lambda p: f"share-{len(f.calls('create_recipe_share'))}"
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: f)
    api._share_check_limiter.reset()
    api._idempotency.reset()
    return f


def test_a_repeated_share_answers_the_first_share_and_spends_one_check(client, share_fake):
    c = _as(client, USER)
    body = {"email": "friend@example.com", "recipe_ids": [R1]}
    first = c.post("/shares", json=body, headers={"Idempotency-Key": KEY})
    again = c.post("/shares", json=body, headers={"Idempotency-Key": KEY})
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert len(share_fake.calls("create_recipe_share")) == 1
    assert len(share_fake.calls("user_id_for_email")) == 1


def test_a_refused_share_does_not_spend_the_key(client, share_fake):
    c = _as(client, USER)
    headers = {"Idempotency-Key": KEY}
    nobody = {"email": "nobody@example.com", "recipe_ids": [R1]}
    friend = {"email": "friend@example.com", "recipe_ids": [R1]}
    assert c.post("/shares", json=nobody, headers=headers).status_code == 404
    assert c.post("/shares", json=friend, headers=headers).status_code == 201


def test_cors_allows_the_header():
    preflight = TestClient(api.app).options("/jobs", headers={
        "Origin": api._cors_origins[0],
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
    })
    assert preflight.status_code == 200
    assert "idempotency-key" in preflight.headers["access-control-allow-headers"].lower()
