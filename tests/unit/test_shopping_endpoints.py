"""POST /shopping/classify through the app: auth, the caps, the 502, the shape.

The classify module's own checks are proved in test_shopping_classify.py; here
the module boundary is the app and a scripted Gemini client.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

import recipeparser.adapters.api as api
from recipeparser.adapters.shopping_api import (
    CLASSIFY_BUSY,
    CLASSIFY_TIMEOUT,
    CLASSIFY_UNAVAILABLE,
    DUPLICATE_KEYS,
    MAX_INGREDIENTS,
    MAX_KNOWN_FOODS,
    NOT_CONFIGURED,
    TOO_MANY_INGREDIENTS,
    TOO_MANY_KNOWN_FOODS,
)

USER = "11111111-1111-4111-8111-111111111111"

ING = [
    {"key": "r1:i1", "text": "2 cups plain flour", "name": "plain flour",
     "amount": 2.0, "unit": "cup"},
    {"key": "r1:i2", "text": "200ml crème fraîche", "name": "crème fraîche",
     "amount": 200.0, "unit": "ml"},
]

GOOD = json.dumps({"items": [
    {"key": "r1:i1", "food": "flour", "aisle": "dry_goods", "pantry": True,
     "count": None, "count_unit": None},
    {"key": "r1:i2", "food": "crème fraîche", "aisle": "dairy_eggs", "pantry": False,
     "count": None, "count_unit": None},
]})

BAD = json.dumps({"items": []})


class ScriptedClient:
    def __init__(self, texts):
        self._texts = list(texts)
        self.call_count = 0
        outer = self

        class _Models:
            def generate_content(self, *, model, contents, config):
                outer.call_count += 1

                class _R:
                    text = outer._texts.pop(0)

                return _R()

        self.models = _Models()


class RaisingClient:
    """client.models.generate_content always raising the exception *exc_factory*
    builds, counting how many times it was called."""

    def __init__(self, exc_factory):
        self.call_count = 0
        self._exc_factory = exc_factory
        outer = self

        class _Models:
            def generate_content(self, *, model, contents, config):
                outer.call_count += 1
                raise outer._exc_factory()

        self.models = _Models()


def _quota_error() -> Exception:
    """A real, typed google-genai ClientError carrying a 429 — the shape
    generate_recipe_image's busy branch matches, and the text ("quota") that
    gemini._is_rate_limit_error retries on."""
    exc = genai_errors.ClientError.__new__(genai_errors.ClientError)
    exc.code = 429
    exc.status = "RESOURCE_EXHAUSTED"
    exc.message = "quota exceeded"
    exc.args = ("quota exceeded",)
    return exc


@pytest.fixture
def client():
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _as(client: TestClient, user_id: str = USER) -> TestClient:
    api.app.dependency_overrides[api._verify_supabase_jwt] = lambda: {"sub": user_id}
    return client


def _post(client, ingredients=ING, known_foods=("flour",)):
    return client.post("/shopping/classify",
                       json={"ingredients": list(ingredients), "known_foods": list(known_foods)})


def test_no_token_is_401(client, monkeypatch):
    # An earlier suite member may have reloaded api with DISABLE_AUTH engaged
    # (the worker-lifespan tests); pin the verifying path so this test does not
    # depend on suite ordering.
    monkeypatch.setattr(api, "_DISABLE_AUTH", False)
    response = client.post("/shopping/classify", json={"ingredients": [], "known_foods": []})
    assert response.status_code in (401, 403)  # HTTPBearer's refusal


def test_a_good_call_answers_the_items(client, monkeypatch):
    scripted = ScriptedClient([GOOD])
    monkeypatch.setattr(api, "_get_client", lambda: scripted)
    response = _post(_as(client))
    assert response.status_code == 200
    items = response.json()["items"]
    assert [i["key"] for i in items] == ["r1:i1", "r1:i2"]
    assert items[1]["food"] == "crème fraîche"
    assert scripted.call_count == 1


def test_too_many_ingredients_is_400_without_a_call(client, monkeypatch):
    scripted = ScriptedClient([])
    monkeypatch.setattr(api, "_get_client", lambda: scripted)
    many = [{**ING[0], "key": f"k{n}"} for n in range(MAX_INGREDIENTS + 1)]
    response = _post(_as(client), ingredients=many)
    assert response.status_code == 400
    assert response.json()["detail"] == TOO_MANY_INGREDIENTS
    assert scripted.call_count == 0


def test_too_many_known_foods_is_400_without_a_call(client, monkeypatch):
    scripted = ScriptedClient([])
    monkeypatch.setattr(api, "_get_client", lambda: scripted)
    response = _post(_as(client), known_foods=[f"food{n}" for n in range(MAX_KNOWN_FOODS + 1)])
    assert response.status_code == 400
    assert response.json()["detail"] == TOO_MANY_KNOWN_FOODS
    assert scripted.call_count == 0


def test_a_duplicate_key_is_400_without_a_call(client, monkeypatch):
    scripted = ScriptedClient([])
    monkeypatch.setattr(api, "_get_client", lambda: scripted)
    response = _post(_as(client), ingredients=[ING[0], ING[0]])
    assert response.status_code == 400
    assert response.json()["detail"] == DUPLICATE_KEYS
    assert scripted.call_count == 0


def test_an_empty_generate_answers_empty_without_a_call(client, monkeypatch):
    scripted = ScriptedClient([])
    monkeypatch.setattr(api, "_get_client", lambda: scripted)
    response = _post(_as(client), ingredients=[])
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert scripted.call_count == 0


def test_two_failed_checks_are_502_with_the_reason(client, monkeypatch):
    scripted = ScriptedClient([BAD, BAD])
    monkeypatch.setattr(api, "_get_client", lambda: scripted)
    response = _post(_as(client))
    assert response.status_code == 502
    assert "key" in response.json()["detail"].lower()
    assert scripted.call_count == 2


def test_a_failed_check_then_a_good_reply_is_200(client, monkeypatch):
    scripted = ScriptedClient([BAD, GOOD])
    monkeypatch.setattr(api, "_get_client", lambda: scripted)
    response = _post(_as(client))
    assert response.status_code == 200
    assert scripted.call_count == 2


def test_a_missing_key_is_503_not_configured_without_a_model_call(client, monkeypatch):
    def _no_client():
        raise RuntimeError("no GOOGLE_API_KEY")

    monkeypatch.setattr(api, "_get_client", _no_client)
    response = _post(_as(client))
    assert response.status_code == 503
    assert response.json()["detail"] == NOT_CONFIGURED


def test_a_transport_timeout_is_504(client, monkeypatch):
    raising = RaisingClient(lambda: httpx.TimeoutException("timed out"))
    monkeypatch.setattr(api, "_get_client", lambda: raising)
    response = _post(_as(client))
    assert response.status_code == 504
    assert response.json()["detail"] == CLASSIFY_TIMEOUT
    # A transport timeout is never retried (gemini._call_with_retry's own rule).
    assert raising.call_count == 1


def test_a_rate_limit_error_is_503_busy_after_exactly_one_retry(client, monkeypatch):
    # max_retries=1 (Task 2): one back-off retry, not the full 5x ladder a
    # cook would otherwise wait through.
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda *_: None)
    raising = RaisingClient(_quota_error)
    monkeypatch.setattr(api, "_get_client", lambda: raising)
    response = _post(_as(client))
    assert response.status_code == 503
    assert response.json()["detail"] == CLASSIFY_BUSY
    assert raising.call_count == 2


def test_an_unrecognized_exception_is_503_unavailable(client, monkeypatch):
    # Not a ValueError: classify_ingredients' own check-retry loop catches
    # ValueError for a reply that fails a structural check, which is a
    # different failure (a 502) from this one (the model call itself blowing
    # up with something nobody mapped).
    raising = RaisingClient(lambda: ConnectionError("something else broke"))
    monkeypatch.setattr(api, "_get_client", lambda: raising)
    response = _post(_as(client))
    assert response.status_code == 503
    assert response.json()["detail"] == CLASSIFY_UNAVAILABLE
