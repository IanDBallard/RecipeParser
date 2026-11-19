"""The typed-item aisles call (Cayenne "typed items get a section").

A shopper types an item the household has never placed; it lands in Other, and the
device asks this module, in the background, which aisle it belongs in. The answer is
remembered on the device side (``food_preferences``), so a food is asked once.

One Gemini Decisions request per call: one ``choice`` question per food, the twelve
aisles as the options. The Decisions API answers with calibrated probabilities in one
forward pass and bills input tokens only, which is why typed items use it; recipe
ingredients stay on ``shopping.classify_ingredients``, which also names the food and
counts it - nothing a closed-set decision can produce.

``DecisionsClient`` is shaped like google-genai's ``client.decisions.create`` (same
keyword arguments, same request body) so it can be swapped for the SDK once the SDK's
dependencies fit this service (config.GEMINI_DECISIONS_URL says why it does not yet).
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx
from pydantic import BaseModel

from recipeparser import usage
from recipeparser.config import (
    AISLE_MIN_CONFIDENCE,
    AISLES_TIMEOUT_SECS,
    GEMINI_DECISION_MODEL,
    GEMINI_DECISIONS_URL,
)
from recipeparser.shopping import AISLES, Aisle

log = logging.getLogger(__name__)

#: Extra Decisions calls, beyond the first, spent bisecting a batch the API refused.
AISLES_SPLIT_BUDGET = 20

#: The options every question chooses between: the twelve aisle keys, each described
#: by the label Cayenne shows (domain/aisles.ts) and a few examples. The API sorts
#: option keys itself, so this order is for readers only.
AISLE_CRITERIA: Dict[str, str] = {
    "produce": "Produce: fresh fruit, vegetables, salad and fresh herbs.",
    "meat_fish": "Meat & fish: fresh or chilled meat, poultry, fish and seafood.",
    "dairy_eggs": "Dairy & eggs: milk, butter, cheese, yoghurt, cream and eggs.",
    "bakery": "Bakery: bread, rolls, wraps, cakes and pastries.",
    "dry_goods": "Dry goods & baking: flour, sugar, rice, pasta, grains, cereal, nuts and baking supplies.",
    "spices": "Spices & seasonings: dried herbs, spices, salt, pepper and stock cubes.",
    "condiments": (
        "Oils, sauces & condiments: cooking oils, vinegar, ketchup, mustard, mayonnaise, "
        "soy sauce, jam and spreads."
    ),
    "tins_jars": "Tins & jars: tinned vegetables, beans, fish and tomatoes; jarred sauces and pickles.",
    "frozen": "Frozen: frozen vegetables, frozen meals, ice and ice cream.",
    "drinks": "Drinks: water, juice, soft drinks, tea, coffee, beer and wine.",
    "household": "Household: cleaning products, paper goods, toiletries and other non-food items.",
    "other": "Other: anything that fits none of the aisles above.",
}
assert tuple(AISLE_CRITERIA) == AISLES, "AISLE_CRITERIA must cover exactly the twelve aisles"

_INPUT = (
    "You are sorting a household's shopping list. Each question names one item a "
    "shopper typed; choose the aisle of a typical supermarket where it is found."
)


class AisleItem(BaseModel):
    food: str
    aisle: Aisle


class DecisionsAPIError(RuntimeError):
    """The Decisions API answered a non-2xx status; ``code`` is that status."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class AislesCheckError(RuntimeError):
    """The Decisions reply could not be read as one answer per food; the message is
    the stable sentence the endpoint's 502 carries."""


class DecisionsClient:
    """A minimal client for ``POST /v1beta/decisions`` over httpx.

    ``create`` takes google-genai's ``client.decisions.create`` keyword arguments and
    returns the parsed JSON body. A transport timeout surfaces as
    ``httpx.TimeoutException``; any non-2xx status as ``DecisionsAPIError``.
    """

    def __init__(self, api_key: str, url: str = GEMINI_DECISIONS_URL) -> None:
        self._api_key = api_key
        self._url = url

    def create(
        self,
        *,
        model: str,
        input: Any,  # noqa: A002 - the API's own field name
        questions: Mapping[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        response = httpx.post(
            self._url,
            headers={"x-goog-api-key": self._api_key, "content-type": "application/json"},
            json={"model": model, "input": input, "questions": dict(questions)},
            timeout=timeout,
        )
        if response.status_code >= 400:
            # The body names the rule a 400 broke or the safety block; it is for the
            # log, never the shopper.
            raise DecisionsAPIError(response.status_code, response.text[:500])
        return response.json()


def _question_key(index: int) -> str:
    # Question keys allow [A-Za-z0-9_-]{1,64}; a food does not, so questions are keyed
    # by position. The model never sees the keys.
    return f"f{index}"


def build_questions(foods: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    return {
        _question_key(i): {
            "type": "choice",
            "instructions": (
                f"Which supermarket aisle is this shopping-list item found in: "
                f"{json.dumps(food, ensure_ascii=False)}?"
            ),
            "criteria": AISLE_CRITERIA,
        }
        for i, food in enumerate(foods)
    }


def _record_usage(reply: Mapping[str, Any]) -> None:
    """Record the call into the current usage context. Decisions bill input tokens only
    (output is always 0); its usage block uses the Interactions field names."""
    raw = reply.get("usage") if isinstance(reply, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}
    usage.record(
        "aisles",
        GEMINI_DECISION_MODEL,
        SimpleNamespace(
            prompt_token_count=raw.get("total_input_tokens"),
            candidates_token_count=raw.get("total_output_tokens"),
            thoughts_token_count=None,
        ),
    )


def decide_aisles(client: Any, foods: Sequence[str]) -> List[AisleItem]:
    """One Decisions request; one ``AisleItem`` per food, in the order given.

    A winning aisle whose confidence is under ``AISLE_MIN_CONFIDENCE`` is answered
    ``other`` (config says why). A reply missing an answer, or naming an option that is
    not an aisle, raises ``AislesCheckError``.
    """
    reply = client.create(
        model=GEMINI_DECISION_MODEL,
        input=_INPUT,
        questions=build_questions(foods),
        timeout=AISLES_TIMEOUT_SECS,
    )
    _record_usage(reply)
    answers = reply.get("answers") if isinstance(reply, Mapping) else None
    if not isinstance(answers, Mapping):
        log.warning("Aisles reply had no answers: %.300s", reply)
        raise AislesCheckError("the aisle decision came back without answers")
    items: List[AisleItem] = []
    for i, food in enumerate(foods):
        answer = answers.get(_question_key(i))
        choice = answer.get("choice") if isinstance(answer, Mapping) else None
        confidence = answer.get("confidence") if isinstance(answer, Mapping) else None
        if choice not in AISLES or not isinstance(confidence, (int, float)):
            log.warning("Aisles reply unreadable for %s: %.300s", _question_key(i), answer)
            raise AislesCheckError("the aisle decision came back without an answer for every food")
        aisle = choice if confidence >= AISLE_MIN_CONFIDENCE else "other"
        items.append(AisleItem(food=food, aisle=aisle))
    return items


def decide_aisles_splitting(
    client: Any, foods: Sequence[str], budget: int = AISLES_SPLIT_BUDGET
) -> List[AisleItem]:
    """``decide_aisles``, but a 400 (a safety block names no food) bisects the batch.

    A refused single food answers ``other``; the rest of the batch still sorts. At most
    ``budget`` calls are made beyond the first; when it is spent the refusal that hit
    the limit is re-raised. Any other error propagates unchanged. The answer is in the
    order of *foods*.
    """
    remaining = [budget]

    def solve(batch: Sequence[str], refusal: Optional[DecisionsAPIError]) -> List[AisleItem]:
        # *refusal* is the 400 that made us split; it is re-raised if no budget is left.
        if refusal is not None:
            if remaining[0] <= 0:
                raise refusal
            remaining[0] -= 1
        try:
            return decide_aisles(client, batch)
        except DecisionsAPIError as exc:
            if exc.code != 400:
                raise
            log.warning("Aisles request of %d food(s) refused (400)", len(batch))
            if len(batch) == 1:
                return [AisleItem(food=batch[0], aisle="other")]
            mid = len(batch) // 2
            return solve(batch[:mid], exc) + solve(batch[mid:], exc)

    return solve(list(foods), None)
