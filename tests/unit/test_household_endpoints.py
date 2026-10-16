"""The household endpoints (Cayenne's household shopping design, Part 3).

Every change is one database function (Stage 3a), so these tests pin what each endpoint sends
its function and how it answers each outcome; the functions themselves are proved by Cayenne's
CI probes, not here.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import recipeparser.adapters.api as api
from tests.unit.share_fakes import FakeSupabase

ME = "11111111-1111-4111-8111-111111111111"
PARTNER = "22222222-2222-4222-8222-222222222222"
INVITE = "44444444-4444-4444-8444-444444444444"
HOUSEHOLD = "77777777-7777-4777-8777-777777777777"
RECIPE = "55555555-5555-4555-8555-555555555551"
JOB = "66666666-6666-4666-8666-666666666666"
EMAILS = {ME: "cook@example.com", PARTNER: "partner@example.com"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeSupabase()
    by_email = {v: k for k, v in EMAILS.items()}
    f.rpcs["user_id_for_email"] = lambda p: by_email.get(p["p_email"])
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: f)
    api._share_check_limiter.reset()
    api._idempotency.reset()
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


def _raises(code: str, message: str = "refused"):
    def handler(_p):
        raise APIError({"code": code, "message": message, "details": None, "hint": None})
    return handler


# ── invite ─────────────────────────────────────────────────────────────────

def test_an_invite_is_made_through_the_function_and_answers_201(client, fake):
    fake.rpcs["create_household_invite"] = lambda p: INVITE
    r = _as(client, ME).post("/household/invites", json={"email": " Partner@Example.com "})
    assert r.status_code == 201
    assert r.json() == {"invite_id": INVITE}
    assert fake.calls("create_household_invite") == [{
        "p_inviter": ME, "p_inviter_email": "cook@example.com",
        "p_invitee": PARTNER, "p_invitee_email": "partner@example.com",
    }]


def test_an_invite_to_no_account_is_404_and_calls_nothing(client, fake):
    r = _as(client, ME).post("/household/invites", json={"email": "nobody@example.com"})
    assert (r.status_code, r.json()["detail"]) == (404, "No Cayenne account uses that email.")
    assert fake.calls("create_household_invite") == []


def test_inviting_yourself_is_422(client, fake):
    r = _as(client, ME).post("/household/invites", json={"email": "COOK@example.com"})
    assert (r.status_code, r.json()["detail"]) == (422, "That's your own email.")


def test_an_invite_without_an_at_sign_is_422(client, fake):
    r = _as(client, ME).post("/household/invites", json={"email": "partner"})
    assert (r.status_code, r.json()["detail"]) == (422, "Enter an email address.")


@pytest.mark.parametrize("message, detail", [
    ("already a member", "They're already in your household."),
    ("already invited", "They already have an invite to your household."),
])
def test_an_invite_the_function_refuses_as_a_duplicate_is_409(client, fake, message, detail):
    fake.rpcs["create_household_invite"] = _raises("23505", message)
    r = _as(client, ME).post("/household/invites", json={"email": "partner@example.com"})
    assert (r.status_code, r.json()["detail"]) == (409, detail)


def test_a_full_household_is_422_in_the_design_s_words(client, fake):
    fake.rpcs["create_household_invite"] = _raises("23514", "household full")
    r = _as(client, ME).post("/household/invites", json={"email": "partner@example.com"})
    assert (r.status_code, r.json()["detail"]) == (422, "A household has at most 8 people, counting invites.")


def test_an_invite_with_no_email_claim_is_422_before_any_lookup(client, fake):
    r = _as(client, ME, email=None).post("/household/invites", json={"email": "partner@example.com"})
    assert (r.status_code, r.json()["detail"]) == (422, "Your account has no email address.")
    assert fake.calls("user_id_for_email") == []


def test_a_retried_invite_replays_its_answer_without_a_second_check(client, fake):
    fake.rpcs["create_household_invite"] = lambda p: INVITE
    # A valid key (8-128 letters, digits or hyphens: adapters/idempotency.py's _KEY).
    headers = {"Idempotency-Key": "invite-retry-0001"}
    first = _as(client, ME).post("/household/invites", json={"email": "partner@example.com"}, headers=headers)
    again = client.post("/household/invites", json={"email": "partner@example.com"}, headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == {"invite_id": INVITE}
    assert len(fake.calls("user_id_for_email")) == 1
    assert len(fake.calls("create_household_invite")) == 1


def test_the_twenty_first_lookup_in_an_hour_is_429(client, fake):
    fake.rpcs["create_household_invite"] = _raises("23505", "already invited")
    for _ in range(20):
        _as(client, ME).post("/household/invites", json={"email": "partner@example.com"})
    r = client.post("/household/invites", json={"email": "partner@example.com"})
    assert (r.status_code, r.json()["detail"]) == (429, "Too many checks. Try again in an hour.")


# ── accept, decline, cancel ────────────────────────────────────────────────

def test_accept_answers_the_joined_household(client, fake):
    fake.rpcs["accept_household_invite"] = lambda p: HOUSEHOLD
    r = _as(client, PARTNER).post(f"/household/invites/{INVITE}/accept")
    assert (r.status_code, r.json()) == (200, {"household_id": HOUSEHOLD})
    assert fake.calls("accept_household_invite") == [{"p_invite": INVITE, "p_invitee": PARTNER}]


def test_accept_of_an_invite_that_is_not_open_and_yours_is_404(client, fake):
    fake.rpcs["accept_household_invite"] = lambda p: None
    r = _as(client, PARTNER).post(f"/household/invites/{INVITE}/accept")
    assert (r.status_code, r.json()["detail"]) == (404, "This invite is no longer open.")


@pytest.mark.parametrize("action, status_word, actor", [("decline", "declined", PARTNER), ("cancel", "cancelled", ME)])
def test_decline_and_cancel_close_through_one_function(client, fake, action, status_word, actor):
    fake.rpcs["close_household_invite"] = lambda p: True
    r = _as(client, actor).post(f"/household/invites/{INVITE}/{action}")
    assert (r.status_code, r.json()) == (200, {"status": status_word})
    assert fake.calls("close_household_invite") == [{"p_invite": INVITE, "p_actor": actor, "p_status": status_word}]


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_closing_an_invite_that_is_not_open_is_404(client, fake, action):
    fake.rpcs["close_household_invite"] = lambda p: False
    r = _as(client, ME).post(f"/household/invites/{INVITE}/{action}")
    assert (r.status_code, r.json()["detail"]) == (404, "This invite is no longer open.")


def test_a_malformed_invite_id_is_422_and_calls_nothing(client, fake):
    r = _as(client, ME).post("/household/invites/not-a-uuid/accept")
    assert r.status_code == 422
    assert fake.calls("accept_household_invite") == []


# ── leave and remove ───────────────────────────────────────────────────────

def test_leave_is_leave_household_with_the_caller_twice(client, fake):
    fake.rpcs["leave_household"] = lambda p: HOUSEHOLD
    r = _as(client, ME).post("/household/leave")
    assert (r.status_code, r.json()) == (200, {"household_id": HOUSEHOLD})
    assert fake.calls("leave_household") == [{"p_user": ME, "p_actor": ME}]


def test_leaving_a_household_of_one_is_409(client, fake):
    fake.rpcs["leave_household"] = _raises("23514", "household of one")
    r = _as(client, ME).post("/household/leave")
    assert (r.status_code, r.json()["detail"]) == (409, "You're the only one in your household.")


def test_remove_names_the_member_and_the_caller_as_actor(client, fake):
    fake.rpcs["leave_household"] = lambda p: HOUSEHOLD
    r = _as(client, ME).post(f"/household/members/{PARTNER}/remove")
    assert (r.status_code, r.json()) == (200, {"household_id": HOUSEHOLD})
    assert fake.calls("leave_household") == [{"p_user": PARTNER, "p_actor": ME}]


def test_removing_someone_not_in_your_household_is_404(client, fake):
    fake.rpcs["leave_household"] = lambda p: None
    r = _as(client, ME).post(f"/household/members/{PARTNER}/remove")
    assert (r.status_code, r.json()["detail"]) == (404, "They're not in your household.")


def test_removing_yourself_is_404_and_calls_nothing(client, fake):
    r = _as(client, ME).post(f"/household/members/{ME}/remove")
    assert (r.status_code, r.json()["detail"]) == (404, "They're not in your household.")
    assert fake.calls("leave_household") == []


# ── copy ───────────────────────────────────────────────────────────────────

def test_copy_is_one_call_and_answers_the_job(client, fake):
    fake.rpcs["copy_household_recipe"] = lambda p: JOB
    r = _as(client, ME).post("/household/copy", json={"recipe_id": RECIPE})
    assert (r.status_code, r.json()) == (202, {"job_id": JOB})
    assert fake.calls("copy_household_recipe") == [{"p_caller": ME, "p_caller_email": "cook@example.com", "p_recipe": RECIPE}]


def test_copy_of_a_recipe_that_is_not_a_member_s_pin_is_404(client, fake):
    fake.rpcs["copy_household_recipe"] = lambda p: None
    r = _as(client, ME).post("/household/copy", json={"recipe_id": RECIPE})
    assert (r.status_code, r.json()["detail"]) == (404, "This recipe is no longer pinned in your household.")


def test_copy_with_no_email_claim_is_422(client, fake):
    r = _as(client, ME, email=None).post("/household/copy", json={"recipe_id": RECIPE})
    assert (r.status_code, r.json()["detail"]) == (422, "Your account has no email address.")


# ── errors common to all ───────────────────────────────────────────────────

def test_an_unexpected_database_error_is_a_503_not_a_refusal(client, fake):
    fake.rpcs["leave_household"] = _raises("57014", "canceling statement due to statement timeout")
    r = _as(client, ME).post("/household/leave")
    assert (r.status_code, r.json()["detail"]) == (503, "Could not reach the household service. Try again.")


def test_no_service_client_is_503(client, monkeypatch):
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: None)
    r = _as(client, ME).post("/household/leave")
    assert (r.status_code, r.json()["detail"]) == (503, "Households are not configured on this server.")


def test_every_endpoint_needs_a_token(client, monkeypatch):
    monkeypatch.setattr(api, "_DISABLE_AUTH", False)
    for path, body in [("/household/invites", {"email": "x@y.z"}), (f"/household/invites/{INVITE}/accept", None),
                       (f"/household/invites/{INVITE}/decline", None), (f"/household/invites/{INVITE}/cancel", None),
                       ("/household/leave", None), (f"/household/members/{PARTNER}/remove", None),
                       ("/household/copy", {"recipe_id": RECIPE})]:
        assert client.post(path, json=body).status_code == 401, path
