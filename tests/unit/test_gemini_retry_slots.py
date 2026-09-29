"""Fix Roadmap F-109: Gemini's own retries take a rate-limiter slot. No real API calls — the client is a stub.

``_call_with_retry`` (429 / 5xx back-off) and ``_generate_and_parse`` (a reply that will not parse) made
further requests without a slot: F-012's gap one level down. The caller takes the first attempt's slot;
every later request takes its own, and a caller that passes no limiter behaves as before.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from google.genai import errors as genai_errors

from recipeparser import gemini
from recipeparser.core.stages.extract import extract
from recipeparser.core.stages.refine import refine
from recipeparser.models import CayenneRefinement, RecipeExtraction, TokenizedDirection

VALID = '{"recipes": []}'
TRUNCATED = '{"recipes": [{"title": "Half a rec'


def _reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason="STOP")])


def _rate_limited() -> Exception:
    return genai_errors.ClientError(429, {"message": "429 RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"})


class _Log:
    """A limiter and a stub client writing to one log, so the order of slot and request shows."""

    def __init__(self, *outcomes):
        self.log = []
        self.limiter = MagicMock()
        self.limiter.wait_then_record_start.side_effect = lambda: self.log.append("slot")
        self.client = MagicMock()
        replies = iter(outcomes)

        def request(**_kw):
            self.log.append("request")
            outcome = next(replies)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self.client.models.generate_content.side_effect = request


def _no_sleep(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)


def test_a_backoff_retry_takes_a_slot_and_the_first_attempt_none(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _Log(_rate_limited(), gemini.genai_errors.ServerError(503, {"message": "x", "status": "UNAVAILABLE"}),
                 _reply("ok"))
    gemini._call_with_retry(calls.client, model="m", contents="c", config={}, limiter=calls.limiter)
    assert calls.log == ["request", "slot", "request", "slot", "request"]


def test_a_parse_retry_takes_a_slot(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _Log(_reply(TRUNCATED), _reply(VALID))
    gemini.extract_recipes("chunk", calls.client, limiter=calls.limiter)
    assert calls.log == ["request", "slot", "request"]


def test_a_parse_retry_after_a_backoff_retry_takes_one_slot_per_request(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _Log(_rate_limited(), _reply(TRUNCATED), _reply(VALID))
    gemini.extract_recipe_from_text("text", calls.client, limiter=calls.limiter)
    assert calls.log == ["request", "slot", "request", "slot", "request"]


def test_with_no_limiter_the_retries_still_run(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _Log(_rate_limited(), _reply(TRUNCATED), _reply(VALID))
    assert gemini.extract_recipes("chunk", calls.client).recipes == []
    assert calls.log == ["request", "request", "request"]


def test_the_bakers_table_retry_takes_a_slot(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _Log(_rate_limited(), _reply("500g flour"))
    assert gemini.normalise_baker_table("Flour 100%", calls.client, limiter=calls.limiter) == "500g flour"
    assert calls.log == ["request", "slot", "request"]


def test_extract_hands_its_limiter_to_the_gemini_retries(monkeypatch):
    # extract() takes the first request's slot (F-012); the parse retry inside takes the second.
    _no_sleep(monkeypatch)
    calls = _Log(_reply(TRUNCATED), _reply(VALID))
    extract("Scones ...", client=calls.client, limiter=calls.limiter)
    assert calls.log == ["slot", "request", "slot", "request"]


def test_refine_hands_its_limiter_to_the_refinement_call():
    refinement = CayenneRefinement(title="T", base_servings=1, structured_ingredients=[],
                                   tokenized_directions=[TokenizedDirection(step=1, text="Mix.")])
    limiter = MagicMock()
    raw = RecipeExtraction(name="T", ingredients=[], directions=["Mix."])
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement) as fn:
        refine(raw, client=MagicMock(), limiter=limiter)
    assert fn.call_args.kwargs["limiter"] is limiter


def test_the_refinement_backoff_retry_takes_a_slot(monkeypatch):
    _no_sleep(monkeypatch)
    body = '{"title": "T", "base_servings": 1, "structured_ingredients": [], ' \
           '"tokenized_directions": [{"step": 1, "text": "Mix."}]}'
    calls = _Log(_rate_limited(), _reply(body))
    raw = RecipeExtraction(name="T", ingredients=[], directions=["Mix."])
    assert gemini.refine_recipe_for_cayenne(raw, calls.client, limiter=calls.limiter) is not None
    assert calls.log == ["request", "slot", "request"]
