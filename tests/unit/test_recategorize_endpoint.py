"""POST /jobs/recategorize and the cancel branch for kind = 'recategorize' (spec 6.1, 6.3).

The client cannot queue or cancel these jobs itself: ingestion_jobs carries a
SELECT-only policy, so the PowerSync insert the specification first described is
rejected 42501 and dropped silently. These endpoints are the replacement, and
the two things they must never delegate to a device are the ownership check and
the subtree expansion.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import recipeparser.adapters.api as api

OWNER = "11111111-1111-1111-1111-111111111111"
STRANGER = "22222222-2222-2222-2222-222222222222"

# Cuisine > Asian > Thai, plus an unrelated root.
CATS = [
    {"id": "A", "name": "Cuisine", "parent_id": None},
    {"id": "F", "name": "Asian", "parent_id": "A"},
    {"id": "L", "name": "Thai", "parent_id": "F"},
    {"id": "Q", "name": "Quick", "parent_id": None},
]


class _Query:
    def __init__(self, fake, table):
        self.fake, self.table, self.ops = fake, table, []

    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return _op

    def execute(self):
        self.fake.queries.append(self)
        handler = self.fake.handlers.get(self.table)
        return MagicMock(data=handler(self) if handler else self.fake.responses.get(self.table, []))


class FakeSupabase:
    def __init__(self):
        self.responses: Dict[str, Any] = {}
        self.handlers: Dict[str, Any] = {}
        self.queries: List[_Query] = []

    def table(self, name):
        return _Query(self, name)


def _inserted(fake):
    for q in fake.queries:
        if q.table == "ingestion_jobs":
            for name, args, _ in q.ops:
                if name == "insert":
                    return args[0]
    return None


@pytest.fixture
def fake(monkeypatch):
    f = FakeSupabase()

    # The fake honours the user fence, or the ownership test proves nothing: a
    # double that hands every caller the same rows cannot tell a real check from
    # a missing one.
    def categories(q):
        wanted = next((o[1][1] for o in q.ops if o[0] == "eq" and o[1][0] == "user_id"), None)
        return CATS if wanted == OWNER else []

    f.handlers["categories"] = categories
    f.responses["ingestion_jobs"] = []
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: f)
    return f


@pytest.fixture
def client():
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _as(user_id: str):
    return lambda: {"sub": user_id}


def _post(client, body, user=OWNER):
    api.app.dependency_overrides[api._verify_supabase_jwt] = _as(user)
    return client.post("/jobs/recategorize", json=body)


# ── queueing ────────────────────────────────────────────────────────────────

def test_queues_a_pending_job_and_answers_202(client, fake):
    response = _post(client, {"category_ids": ["Q"]})
    assert response.status_code == 202
    assert response.json()["job_id"]
    row = _inserted(fake)
    assert row["kind"] == "recategorize"
    # pending, not running: the worker's claim is the compare-and-swap that stops
    # two workers taking the same job.
    assert row["status"] == "pending" and row["stage"] == "IDLE"
    assert row["user_id"] == OWNER
    assert row["params"] == {"category_ids": ["Q"]}
    assert row["source_hint"] == "Quick"
    # Initialised exactly as the ingest insert does, or the worker's first
    # skipped-write would be the column's first use.
    assert row["skipped"] == [] and row["skipped_count"] == 0


def test_a_folder_offers_its_subtree(client, fake):
    # D4: expansion happens here, not on the device, and uses the same walk the
    # worker builds its axes from, so the job and the prompt cannot drift apart.
    response = _post(client, {"category_ids": ["A"]})
    assert response.status_code == 202
    assert _inserted(fake)["params"]["category_ids"] == ["A", "F", "L"]


def test_a_mid_level_folder_offers_only_what_is_beneath_it(client, fake):
    _post(client, {"category_ids": ["F"]})
    assert _inserted(fake)["params"]["category_ids"] == ["F", "L"]


def test_duplicate_ids_are_collapsed(client, fake):
    _post(client, {"category_ids": ["A", "A", "F"]})
    assert _inserted(fake)["params"]["category_ids"] == ["A", "F", "L"]


# ── refusals ────────────────────────────────────────────────────────────────

def test_an_id_the_caller_does_not_own_is_refused(client, fake):
    # The stranger's category query returns nothing, so every id is unknown.
    response = _post(client, {"category_ids": ["A"]}, user=STRANGER)
    assert response.status_code == 422
    assert _inserted(fake) is None


def test_an_unknown_id_is_refused_without_echoing_it(client, fake):
    response = _post(client, {"category_ids": ["A", "ghost"]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    # The count, not the ids: this is the one endpoint where a caller names rows
    # it may not own, so the reply must not confirm which of a guessed set exist.
    assert "1 of 2" in detail
    assert "ghost" not in detail
    assert _inserted(fake) is None


def test_an_empty_list_is_refused_rather_than_queued(client, fake):
    assert _post(client, {"category_ids": []}).status_code == 422
    assert _inserted(fake) is None


def test_no_token_is_rejected(client, fake, monkeypatch):
    monkeypatch.setattr(api, "_DISABLE_AUTH", False)
    api.app.dependency_overrides.clear()
    assert client.post("/jobs/recategorize", json={"category_ids": ["A"]}).status_code == 401


# ── cancel ──────────────────────────────────────────────────────────────────

def _cancel(client, fake, job_row, user=OWNER):
    fake.handlers["ingestion_jobs"] = lambda q: (
        [job_row] if any(o[0] == "select" for o in q.ops) and job_row else []
    )
    api.app.dependency_overrides[api._verify_supabase_jwt] = _as(user)
    return client.post("/jobs/job-1/cancel")


def test_cancelling_a_running_recategorise_job_writes_the_status(client, fake):
    response = _cancel(client, fake, {"id": "job-1", "status": "running", "kind": "recategorize"})
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    updates = [args[0] for q in fake.queries if q.table == "ingestion_jobs"
               for name, args, _ in q.ops if name == "update"]
    assert updates and updates[0]["status"] == "cancelled"


def test_cancelling_a_pending_recategorise_job_is_allowed(client, fake):
    assert _cancel(client, fake, {"id": "job-1", "status": "pending", "kind": "recategorize"}).status_code == 200


def test_cancelling_a_finished_recategorise_job_is_409(client, fake):
    response = _cancel(client, fake, {"id": "job-1", "status": "done", "kind": "recategorize"})
    assert response.status_code == 409
    updates = [args[0] for q in fake.queries if q.table == "ingestion_jobs"
               for name, args, _ in q.ops if name == "update"]
    assert not updates, "a terminal job must not be rewritten"


def test_a_job_that_is_not_a_recategorise_job_falls_through_to_the_controller(client, fake):
    # No row comes back (the query is fenced on kind), so the in-memory path runs
    # and answers 404 for an id it does not hold -- the ingest behaviour, unchanged.
    assert _cancel(client, fake, None).status_code == 404
