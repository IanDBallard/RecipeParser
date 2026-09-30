"""The recipe-sharing endpoints (Cayenne's recipe-sharing design, Part 2).

Every change is one database function (the database plan, ruling 1), so these tests pin
what the endpoint sends each function and how it answers each outcome: the function's
own behaviour is proved by Cayenne's CI probes, not here.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

import recipeparser.adapters.api as api
from tests.unit.share_fakes import FakeSupabase

SENDER = "11111111-1111-4111-8111-111111111111"
RECIPIENT = "22222222-2222-4222-8222-222222222222"
STRANGER = "33333333-3333-4333-8333-333333333333"
SHARE = "44444444-4444-4444-8444-444444444444"
R1 = "55555555-5555-4555-8555-555555555551"
R2 = "55555555-5555-4555-8555-555555555552"
JOB = "66666666-6666-4666-8666-666666666666"

EMAILS = {SENDER: "cook@example.com", RECIPIENT: "friend@example.com", STRANGER: "nosy@example.com"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeSupabase()
    by_email = {v: k for k, v in EMAILS.items()}
    f.rpcs["user_id_for_email"] = lambda p: by_email.get(p["p_email"])
    f.tables["recipe_shares"] = [{"id": SHARE, "sender_id": SENDER, "recipient_id": RECIPIENT}]
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: f)
    api._share_check_limiter.reset()
    return f


@pytest.fixture
def client():
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _as(client: TestClient, user_id: str, email: Any = "default") -> TestClient:
    claims: Dict[str, Any] = {"sub": user_id}
    if email == "default":
        claims["email"] = EMAILS[user_id]
    elif email is not None:
        claims["email"] = email
    api.app.dependency_overrides[api._verify_supabase_jwt] = lambda: claims
    return client


# ── POST /shares/recipient ───────────────────────────────────────────────────

def test_a_known_address_is_confirmed_normalised(client, fake):
    response = _as(client, SENDER).post("/shares/recipient", json={"email": "  Friend@Example.COM "})
    assert response.status_code == 200
    assert response.json() == {"email": "friend@example.com"}
    assert fake.calls("user_id_for_email") == [{"p_email": "friend@example.com"}]


def test_an_unknown_address_is_404_with_the_spec_sentence(client, fake):
    response = _as(client, SENDER).post("/shares/recipient", json={"email": "nobody@example.com"})
    assert response.status_code == 404
    assert response.json()["detail"] == "No Cayenne account uses that email."


def test_ones_own_address_is_422_without_a_lookup(client, fake):
    response = _as(client, SENDER).post("/shares/recipient", json={"email": "COOK@example.com"})
    assert response.status_code == 422
    assert response.json()["detail"] == "That's your own email."
    assert fake.calls("user_id_for_email") == []


def test_an_address_that_resolves_to_the_caller_is_422(client, fake):
    # A token with no email claim cannot be compared by address; the account id still is.
    response = _as(client, SENDER, email=None).post("/shares/recipient", json={"email": "cook@example.com"})
    assert response.status_code == 422
    assert response.json()["detail"] == "That's your own email."


def test_a_malformed_address_is_422_and_spends_no_check(client, fake):
    assert _as(client, SENDER).post("/shares/recipient", json={"email": "not-an-address"}).status_code == 422
    assert fake.calls("user_id_for_email") == []


def test_the_twenty_first_check_in_an_hour_is_429(client, fake):
    c = _as(client, SENDER)
    for _ in range(20):
        assert c.post("/shares/recipient", json={"email": "nobody@example.com"}).status_code == 404
    response = c.post("/shares/recipient", json={"email": "friend@example.com"})
    assert response.status_code == 429
    assert response.json()["detail"] == "Too many checks. Try again in an hour."
    assert len(fake.calls("user_id_for_email")) == 20


def test_the_limit_is_per_sender(client, fake):
    for _ in range(20):
        _as(client, SENDER).post("/shares/recipient", json={"email": "nobody@example.com"})
    assert _as(client, RECIPIENT).post("/shares/recipient", json={"email": "cook@example.com"}).status_code == 200


def test_the_limit_forgets_checks_older_than_an_hour():
    from recipeparser.adapters.shares_api import RecipientCheckLimiter
    now = [0.0]
    limiter = RecipientCheckLimiter(limit=2, window=3600.0, clock=lambda: now[0])
    assert limiter.allow("u") and limiter.allow("u")
    assert not limiter.allow("u")
    now[0] = 3600.5
    assert limiter.allow("u")


def test_no_token_is_401(client, fake, monkeypatch):
    monkeypatch.setattr(api, "_DISABLE_AUTH", False)
    assert client.post("/shares/recipient", json={"email": "friend@example.com"}).status_code == 401


def test_no_service_client_is_503(client, monkeypatch):
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: None)
    response = _as(client, SENDER).post("/shares/recipient", json={"email": "friend@example.com"})
    assert response.status_code == 503


# ── POST /shares ─────────────────────────────────────────────────────────────

def test_a_share_is_created_through_the_function_and_answers_201(client, fake):
    fake.rpcs["create_recipe_share"] = lambda p: SHARE
    response = _as(client, SENDER).post("/shares", json={"email": "Friend@example.com",
                                                        "recipe_ids": [R2, R1, R2]})
    assert response.status_code == 201
    assert response.json() == {"share_id": SHARE}
    assert fake.calls("create_recipe_share") == [{
        "p_sender": SENDER, "p_sender_email": "cook@example.com",
        "p_recipient": RECIPIENT, "p_recipient_email": "friend@example.com",
        # Duplicates removed, first-seen order kept: the order is the items' position.
        "p_recipe_ids": [R2, R1],
    }]


def test_a_recipe_the_sender_does_not_own_is_404_naming_no_id(client, fake):
    fake.rpcs["create_recipe_share"] = lambda p: None
    response = _as(client, SENDER).post("/shares", json={"email": "friend@example.com", "recipe_ids": [R1]})
    assert response.status_code == 404
    assert response.json()["detail"] == "One or more of those recipes were not found."


def test_an_empty_share_is_422_before_any_lookup(client, fake):
    response = _as(client, SENDER).post("/shares", json={"email": "friend@example.com", "recipe_ids": []})
    assert response.status_code == 422
    assert fake.rpc_calls == []


def test_more_than_two_hundred_recipes_is_422(client, fake):
    ids = [f"55555555-5555-4555-8555-{i:012d}" for i in range(201)]
    response = _as(client, SENDER).post("/shares", json={"email": "friend@example.com", "recipe_ids": ids})
    assert response.status_code == 422
    assert "200" in response.json()["detail"]
    assert fake.rpc_calls == []


def test_two_hundred_after_duplicates_are_removed_is_allowed(client, fake):
    fake.rpcs["create_recipe_share"] = lambda p: SHARE
    ids = [f"55555555-5555-4555-8555-{i:012d}" for i in range(200)]
    response = _as(client, SENDER).post("/shares", json={"email": "friend@example.com",
                                                        "recipe_ids": ids + ids[:5]})
    assert response.status_code == 201


def test_a_recipient_whose_account_has_gone_is_404(client, fake):
    fake.rpcs["create_recipe_share"] = lambda p: SHARE
    response = _as(client, SENDER).post("/shares", json={"email": "gone@example.com", "recipe_ids": [R1]})
    assert response.status_code == 404
    assert response.json()["detail"] == "No Cayenne account uses that email."
    assert fake.calls("create_recipe_share") == []


def test_a_sender_without_an_email_claim_is_422(client, fake):
    fake.rpcs["create_recipe_share"] = lambda p: SHARE
    response = _as(client, SENDER, email=None).post("/shares", json={"email": "friend@example.com",
                                                                    "recipe_ids": [R1]})
    assert response.status_code == 422
    assert fake.calls("create_recipe_share") == []


def test_a_malformed_recipe_id_is_422(client, fake):
    response = _as(client, SENDER).post("/shares", json={"email": "friend@example.com", "recipe_ids": ["x"]})
    assert response.status_code == 422
    assert fake.rpc_calls == []


def test_a_database_failure_is_503(client, fake):
    def boom(p):
        raise RuntimeError("connection reset")
    fake.rpcs["create_recipe_share"] = boom
    response = _as(client, SENDER).post("/shares", json={"email": "friend@example.com", "recipe_ids": [R1]})
    assert response.status_code == 503
