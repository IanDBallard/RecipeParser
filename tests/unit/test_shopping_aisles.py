"""POST /shopping/aisles: the Decisions request it builds, the answers it reads, the
caps, the error mapping and CORS - through the app with a scripted Decisions client,
plus DecisionsClient over a mocked httpx transport."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import recipeparser.adapters.api as api
from recipeparser import shopping_aisles
from recipeparser.adapters.shopping_api import (
    AISLES_BUSY,
    AISLES_NOT_CONFIGURED,
    AISLES_TIMEOUT,
    AISLES_UNAVAILABLE,
    BAD_FOOD,
    DUPLICATE_FOODS,
    MAX_AISLE_FOODS,
    NO_FOODS,
    TOO_MANY_FOODS,
)
from recipeparser.config import GEMINI_DECISION_MODEL
from recipeparser.shopping import AISLES
from recipeparser.shopping_aisles import (
    AISLE_CRITERIA,
    DecisionsAPIError,
    DecisionsClient,
)

USER = "11111111-1111-4111-8111-111111111111"


def _answer(choice: str, confidence: float = 0.9) -> dict:
    rest = (1.0 - confidence) / (len(AISLES) - 1)
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": {a: (confidence if a == choice else rest) for a in AISLES},
    }


def _reply(*answers: dict) -> dict:
    return {
        "answers": {f"f{i}": a for i, a in enumerate(answers)},
        "usage": {"total_input_tokens": 321, "total_output_tokens": 0},
    }


class ScriptedDecisions:
    """client.create answering *reply* (or raising what *exc_factory* builds),
    keeping every call's keyword arguments."""

    def __init__(self, reply=None, exc_factory=None):
        self.reply = reply
        self.exc_factory = exc_factory
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc_factory is not None:
            raise self.exc_factory()
        return self.reply


@pytest.fixture
def client():
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch):
    monkeypatch.setattr(api._ai_gate, "check", lambda *a, **k: None)


def _as(client: TestClient, user_id: str = USER) -> TestClient:
    api.app.dependency_overrides[api._verify_supabase_jwt] = lambda: {"sub": user_id}
    return client


def _use(monkeypatch, scripted) -> None:
    monkeypatch.setattr(api, "_get_decisions_client", lambda: scripted)


def _post(client, foods):
    return client.post("/shopping/aisles", json={"foods": list(foods)})


# --- the happy path -----------------------------------------------------------------


def test_a_good_call_answers_each_food_its_aisle_in_order(client, monkeypatch):
    scripted = ScriptedDecisions(_reply(
        _answer("dairy_eggs"), _answer("household"), _answer("produce"),
    ))
    _use(monkeypatch, scripted)
    response = _post(_as(client), ["butter", "dish soap", "lemon"])
    assert response.status_code == 200
    assert response.json() == {"items": [
        {"food": "butter", "aisle": "dairy_eggs"},
        {"food": "dish soap", "aisle": "household"},
        {"food": "lemon", "aisle": "produce"},
    ]}
    assert len(scripted.calls) == 1


def test_order_follows_the_request_not_the_reply(client, monkeypatch):
    # Answers keyed by question, arriving in a different order from the foods.
    reply = {"answers": {
        "f2": _answer("frozen"), "f0": _answer("bakery"), "f1": _answer("drinks"),
    }}
    _use(monkeypatch, ScriptedDecisions(reply))
    response = _post(_as(client), ["bagel", "cola", "peas frozen"])
    assert [(i["food"], i["aisle"]) for i in response.json()["items"]] == [
        ("bagel", "bakery"), ("cola", "drinks"), ("peas frozen", "frozen"),
    ]


def test_one_decisions_request_with_one_choice_question_per_food(client, monkeypatch):
    scripted = ScriptedDecisions(_reply(_answer("dairy_eggs"), _answer("household")))
    _use(monkeypatch, scripted)
    _post(_as(client), ["butter", "dish soap"])
    (call,) = scripted.calls
    assert call["model"] == GEMINI_DECISION_MODEL
    assert call["input"].strip()
    assert set(call["questions"]) == {"f0", "f1"}
    for key, food in (("f0", "butter"), ("f1", "dish soap")):
        q = call["questions"][key]
        assert q["type"] == "choice"
        assert f'"{food}"' in q["instructions"]
        assert q["criteria"] == AISLE_CRITERIA
    assert call["timeout"] > 0


def test_the_criteria_are_the_twelve_aisles_with_cayennes_labels():
    assert tuple(AISLE_CRITERIA) == AISLES
    labels = [
        "Produce", "Meat & fish", "Dairy & eggs", "Bakery", "Dry goods & baking",
        "Spices & seasonings", "Oils, sauces & condiments", "Tins & jars", "Frozen",
        "Drinks", "Household", "Other",
    ]
    for aisle, label in zip(AISLES, labels):
        assert AISLE_CRITERIA[aisle].startswith(label + ":"), aisle


def test_a_low_confidence_winner_is_answered_other(client, monkeypatch):
    monkeypatch.setattr(shopping_aisles, "AISLE_MIN_CONFIDENCE", 0.5)
    _use(monkeypatch, ScriptedDecisions(_reply(_answer("spices", 0.49), _answer("spices", 0.5))))
    response = _post(_as(client), ["zaatar blend", "sumac"])
    assert [i["aisle"] for i in response.json()["items"]] == ["other", "spices"]


def test_the_call_is_attributed_as_classify_step_aisles(client, monkeypatch):
    captured = []
    real = api.usage.attributed

    def spy(user_id, action, job_id=None, *, get_client):
        captured.append((user_id, action))
        return real(user_id, action, job_id, get_client=get_client)

    recorded = []
    real_record = api.usage.record

    def record_spy(step, model, usage_metadata):
        recorded.append((step, model, usage_metadata.prompt_token_count))
        return real_record(step, model, usage_metadata)

    monkeypatch.setattr(api.usage, "attributed", spy)
    monkeypatch.setattr(shopping_aisles.usage, "record", record_spy)
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: None)
    _use(monkeypatch, ScriptedDecisions(_reply(_answer("dairy_eggs"))))
    assert _post(_as(client), ["butter"]).status_code == 200
    assert captured == [(USER, "classify")]
    assert recorded == [("aisles", GEMINI_DECISION_MODEL, 321)]


# --- validation: 400 without a call ----------------------------------------------------


@pytest.mark.parametrize(
    "foods,detail",
    [
        ([], NO_FOODS),
        ([f"food {n}" for n in range(MAX_AISLE_FOODS + 1)], TOO_MANY_FOODS),
        (["butter", "butter"], DUPLICATE_FOODS),
        (["butter", "  "], BAD_FOOD),
        (["x" * 201], BAD_FOOD),
    ],
    ids=["empty", "over-cap", "duplicate", "blank", "too-long"],
)
def test_bad_requests_are_400_without_a_call(client, monkeypatch, foods, detail):
    scripted = ScriptedDecisions(_reply())
    _use(monkeypatch, scripted)
    response = _post(_as(client), foods)
    assert response.status_code == 400
    assert response.json()["detail"] == detail
    assert scripted.calls == []


def test_exactly_the_cap_is_accepted(client, monkeypatch):
    foods = [f"food {n}" for n in range(MAX_AISLE_FOODS)]
    _use(monkeypatch, ScriptedDecisions(_reply(*[_answer("other")] * MAX_AISLE_FOODS)))
    response = _post(_as(client), foods)
    assert response.status_code == 200
    assert [i["food"] for i in response.json()["items"]] == foods


def test_no_token_is_401(client, monkeypatch):
    monkeypatch.setattr(api, "_DISABLE_AUTH", False)
    response = client.post("/shopping/aisles", json={"foods": ["butter"]})
    assert response.status_code in (401, 403)


# --- error mapping ---------------------------------------------------------------------


def test_a_missing_key_is_503_not_configured(client, monkeypatch):
    def _no_client():
        raise RuntimeError("no GOOGLE_API_KEY")

    monkeypatch.setattr(api, "_get_decisions_client", _no_client)
    response = _post(_as(client), ["butter"])
    assert response.status_code == 503
    assert response.json()["detail"] == AISLES_NOT_CONFIGURED


def test_a_transport_timeout_is_504(client, monkeypatch):
    scripted = ScriptedDecisions(exc_factory=lambda: httpx.ReadTimeout("timed out"))
    _use(monkeypatch, scripted)
    response = _post(_as(client), ["butter"])
    assert response.status_code == 504
    assert response.json()["detail"] == AISLES_TIMEOUT
    assert len(scripted.calls) == 1  # never retried


def test_a_429_is_503_busy(client, monkeypatch):
    _use(monkeypatch, ScriptedDecisions(exc_factory=lambda: DecisionsAPIError(429, "quota")))
    response = _post(_as(client), ["butter"])
    assert response.status_code == 503
    assert response.json()["detail"] == AISLES_BUSY


@pytest.mark.parametrize("code", [400, 403, 500, 503])
def test_any_other_refusal_is_503_unavailable(client, monkeypatch, code):
    _use(monkeypatch, ScriptedDecisions(exc_factory=lambda: DecisionsAPIError(code, "no")))
    response = _post(_as(client), ["butter"])
    assert response.status_code == 503
    assert response.json()["detail"] == AISLES_UNAVAILABLE


def test_an_unrecognized_exception_is_503_unavailable(client, monkeypatch):
    _use(monkeypatch, ScriptedDecisions(exc_factory=lambda: ConnectionError("broke")))
    response = _post(_as(client), ["butter"])
    assert response.status_code == 503
    assert response.json()["detail"] == AISLES_UNAVAILABLE


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"answers": {}},
        {"answers": {"f0": _answer("dairy_eggs")}},  # one answer for two foods
        {"answers": {"f0": _answer("dairy_eggs"), "f1": _answer("deli")}},  # not an aisle
        {"answers": {"f0": _answer("dairy_eggs"), "f1": {"type": "choice", "choice": "produce"}}},
    ],
    ids=["no-answers", "empty-answers", "missing-one", "unknown-aisle", "no-confidence"],
)
def test_an_unreadable_reply_is_502(client, monkeypatch, reply):
    _use(monkeypatch, ScriptedDecisions(reply))
    response = _post(_as(client), ["butter", "lemon"])
    assert response.status_code == 502
    assert "aisle decision" in response.json()["detail"]


# --- CORS ------------------------------------------------------------------------------


def test_the_preflight_admits_a_cayenne_origin(client):
    origin = api._cors_origins[0]
    response = client.options(
        "/shopping/aisles",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


# --- DecisionsClient over httpx --------------------------------------------------------


def _mock_httpx(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda req: (seen.append(req), handler(req))[1])
    real_post = httpx.post

    def post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            kwargs.pop("timeout", None)
            return c.post(url, **kwargs)

    monkeypatch.setattr(shopping_aisles.httpx, "post", post)
    assert real_post is not post
    return seen


def test_the_client_posts_the_sdk_body_with_the_key_header(monkeypatch):
    seen = _mock_httpx(monkeypatch, lambda req: httpx.Response(200, json=_reply(_answer("bakery"))))
    reply = DecisionsClient("k-123", url="https://example.test/v1beta/decisions").create(
        model="gemini-decision-1", input="ctx", questions={"f0": {"type": "choice"}}, timeout=5,
    )
    assert reply["answers"]["f0"]["choice"] == "bakery"
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == "https://example.test/v1beta/decisions"
    assert req.headers["x-goog-api-key"] == "k-123"
    assert json.loads(req.content) == {
        "model": "gemini-decision-1", "input": "ctx", "questions": {"f0": {"type": "choice"}},
    }


@pytest.mark.parametrize("code", [400, 429, 500])
def test_the_client_raises_decisions_api_error_on_a_non_2xx(monkeypatch, code):
    _mock_httpx(monkeypatch, lambda req: httpx.Response(code, json={"error": {"message": "no"}}))
    with pytest.raises(DecisionsAPIError) as info:
        DecisionsClient("k").create(model="m", input="i", questions={})
    assert info.value.code == code


def test_the_client_lets_a_timeout_through(monkeypatch):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(httpx.TimeoutException):
        DecisionsClient("k").create(model="m", input="i", questions={})


def test_the_api_factory_needs_the_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        api._get_decisions_client()
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    assert isinstance(api._get_decisions_client(), DecisionsClient)
