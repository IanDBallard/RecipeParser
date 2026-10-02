"""The shopping classify call (Cayenne's shopping list design, Part 3 §2).

One synchronous Gemini call that labels a Generate's ingredients — ``food``,
``aisle``, ``pantry``, ``count``, ``count_unit`` — and nothing else. The device
merges, sums and writes; this module reads and writes no table. Quantities on
the list are computed in code (D5), so the model never sums.

The structural checks live here, with one retry: a reply that fails them is
asked for once more, and the second failure raises ``ClassifyCheckError``,
whose message the endpoint answers as a 502's detail.
"""
from __future__ import annotations

import json
import logging
import math
from typing import List, Literal, Optional, Sequence

from pydantic import BaseModel

from recipeparser.config import CLASSIFY_TIMEOUT_SECS, GEMINI_MODEL
from recipeparser.gemini import _call_with_retry, _schema_for_gemini

log = logging.getLogger(__name__)

#: The twelve aisle keys, in store order (shopping design, *Aisles*). Cayenne's
#: `domain/aisles.ts` and Postgres's checks carry the same list; the response
#: schema below enumerates it so the model cannot answer outside it.
AISLES: tuple[str, ...] = (
    "produce", "meat_fish", "dairy_eggs", "bakery", "dry_goods", "spices",
    "condiments", "tins_jars", "frozen", "drinks", "household", "other",
)

Aisle = Literal[
    "produce", "meat_fish", "dairy_eggs", "bakery", "dry_goods", "spices",
    "condiments", "tins_jars", "frozen", "drinks", "household", "other",
]


class ClassifyIngredient(BaseModel):
    """One scaled ingredient as the device sends it: the key names the
    contribution (``<recipe_id>:<ingredient_id>``), the rest is evidence."""

    key: str
    text: str
    name: str
    amount: Optional[float] = None
    unit: Optional[str] = None


class ClassifiedItem(BaseModel):
    key: str
    food: str
    aisle: Aisle
    pantry: bool
    count: Optional[float] = None
    count_unit: Optional[str] = None


class ClassifyReply(BaseModel):
    items: List[ClassifiedItem]


class ClassifyCheckError(RuntimeError):
    """The model's reply failed a structural check twice; the message is the
    reason the endpoint's 502 carries."""


_PROMPT_HEAD = """You are a grocery shopping classifier. For each ingredient below, say what a \
shopper buys for it.

Rules:
- "food" names what a shopper buys, singular and lower case ("egg", not "Eggs").
- Reuse a food from KNOWN FOODS when the ingredient is the same thing, spelled \
exactly as the known food is.
- Keep a variety only when it changes what is bought: "red onion" stays \
"red onion"; "diced onion" is "onion"; "free-range eggs" is "egg".
- "aisle" is the store aisle the food is found in, one of: {aisles}.
- "pantry" is true for what a typical home kitchen keeps in stock (salt, oil, \
flour, dried spices).
- "count" is how many whole units a shopper buys for this ingredient, in \
"count_unit"'s unit (2.2 onion, 0.3 head garlic, 1 tin). Use null for food \
bought by weight or volume (flour, milk, mince), and then null for \
"count_unit" too.
- Answer every ingredient by its "key", each exactly once.

KNOWN FOODS:
{known_foods}

INGREDIENTS:
{ingredients}
"""


def build_classify_prompt(
    ingredients: Sequence[ClassifyIngredient], known_foods: Sequence[str]
) -> str:
    """The classify prompt. Everything from ``KNOWN FOODS:`` on is the work
    unit — the golden client keys recordings on it (its ``_BODY_MARKERS``)."""
    seen: dict[str, None] = {}
    for food in known_foods:
        cleaned = food.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return _PROMPT_HEAD.format(
        aisles=", ".join(AISLES),
        known_foods=json.dumps(list(seen), ensure_ascii=False),
        ingredients="\n".join(
            json.dumps(i.model_dump(), ensure_ascii=False) for i in ingredients
        ),
    )


def _check(items: List[ClassifiedItem], ingredients: Sequence[ClassifyIngredient]) -> None:
    """Raise ValueError naming the first failed structural check (Part 3 §2)."""
    wanted = [i.key for i in ingredients]
    got = [i.key for i in items]
    if sorted(got) != sorted(wanted):
        raise ValueError(
            f"the reply's keys do not match the request: {len(got)} items for "
            f"{len(wanted)} ingredients, each key required exactly once"
        )
    for item in items:
        if not item.food.strip():
            raise ValueError(f"empty food for key {item.key}")
        if item.aisle not in AISLES:
            raise ValueError(f"unknown aisle {item.aisle!r} for key {item.key}")
        if item.count is not None and (not math.isfinite(item.count) or item.count < 0):
            raise ValueError(f"count must be a finite number >= 0 for key {item.key}")
        has_unit = bool(item.count_unit and item.count_unit.strip())
        if (item.count is None) != (not has_unit):
            raise ValueError(
                f"count_unit must be set exactly when count is, for key {item.key}"
            )


def _once(client, prompt: str) -> List[ClassifiedItem]:
    response = _call_with_retry(
        client,
        model=GEMINI_MODEL,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_json_schema": _schema_for_gemini(ClassifyReply),
            "temperature": 0.1,
            "http_options": {"timeout": CLASSIFY_TIMEOUT_SECS * 1000},
        },
        what="Shopping classify",
    )
    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise ValueError("the model returned an empty reply that cannot be read")
    try:
        reply = ClassifyReply.model_validate(json.loads(text))
    except Exception as exc:
        raise ValueError(f"the model's reply could not be read: {exc}") from exc
    return reply.items


def classify_ingredients(
    client,
    ingredients: Sequence[ClassifyIngredient],
    known_foods: Sequence[str],
) -> List[ClassifiedItem]:
    """Label every ingredient, retrying one failed check (Part 3 §2).

    ``_call_with_retry`` covers transport and quota; this loop covers a reply
    that parses but fails a structural check, as REFINE's parse loop does.
    """
    prompt = build_classify_prompt(ingredients, known_foods)
    last = ""
    for attempt in (1, 2):
        try:
            items = _once(client, prompt)
            _check(items, ingredients)
            return items
        except ValueError as exc:
            last = str(exc)
            log.warning("Classify check failed (attempt %d/2): %s", attempt, last)
    raise ClassifyCheckError(last)
