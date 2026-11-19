"""
recipeparser/adapters/shopping_api.py — the shopping classify endpoint
(Cayenne's shopping list design, Part 3 §2) and the typed-item aisles endpoint
(Cayenne "typed items get a section", recipeparser/shopping_aisles.py).

One synchronous call at Generate: the device sends the scaled ingredients and
the foods it already knows, the model labels them, the device merges. No table
is read or written — any household member's own login serves (household D12).

Kept out of api.py, which is already long and has a refactor queued (Fix
Roadmap F-068). ``build_router`` takes the auth dependency and the Gemini
client factory rather than importing them, so this module never imports api.py
and api.py stays the only place they are defined — the shares_api.py pattern.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from google.genai import errors as genai_errors
from pydantic import BaseModel

from recipeparser import usage
from recipeparser.shopping import (
    ClassifyCheckError,
    ClassifiedItem,
    ClassifyIngredient,
    classify_ingredients,
)
from recipeparser.shopping_aisles import (
    AisleItem,
    AislesCheckError,
    DecisionsAPIError,
    decide_aisles_splitting,
)

log = logging.getLogger(__name__)

MAX_INGREDIENTS = 400
MAX_KNOWN_FOODS = 2000

TOO_MANY_INGREDIENTS = "Too many ingredients for one Generate."
TOO_MANY_KNOWN_FOODS = "Too many known foods for one Generate."
DUPLICATE_KEYS = "Two ingredients carry the same key."

# Mirrors the picture endpoint's error mapping (api.py's
# generate_recipe_image): a cook waiting on Generate gets a sentence, never a
# bare 500 or a raw exception dump.
NOT_CONFIGURED = "The shopping classifier is not configured on this server."
CLASSIFY_TIMEOUT = "The shopping classifier took too long. Try again."
CLASSIFY_BUSY = "The shopping classifier is busy. Try again in a minute."
CLASSIFY_UNAVAILABLE = "The shopping classifier is unavailable. Try again."


class ClassifyRequest(BaseModel):
    ingredients: List[ClassifyIngredient]
    known_foods: List[str]


class ClassifyResponse(BaseModel):
    items: List[ClassifiedItem]


MAX_AISLE_FOODS = 200
MAX_FOOD_CHARS = 200

NO_FOODS = "No foods to sort."
TOO_MANY_FOODS = "Too many foods to sort at once."
BAD_FOOD = "A food is blank or too long."
DUPLICATE_FOODS = "The same food is listed twice."

AISLES_NOT_CONFIGURED = "The aisle sorter is not configured on this server."
AISLES_TIMEOUT = "The aisle sorter took too long. Try again."
AISLES_BUSY = "The aisle sorter is busy. Try again in a minute."
AISLES_UNAVAILABLE = "The aisle sorter is unavailable. Try again."


class AislesRequest(BaseModel):
    foods: List[str]


class AislesResponse(BaseModel):
    items: List[AisleItem]


def build_router(
    verify_jwt: Callable,
    get_client: Callable[[], Any],
    require_ai: Callable,
    get_service_client: Callable[[], Any],
    get_decisions_client: Callable[[], Any],
) -> APIRouter:
    router = APIRouter()

    # A plain def: FastAPI runs it on the thread pool, and the Gemini call is
    # synchronous (as /embed's is).
    @router.post("/shopping/classify", response_model=ClassifyResponse, status_code=200)
    def classify(
        body: ClassifyRequest,
        user: dict = Depends(verify_jwt),
        _gate: None = Depends(require_ai),
    ) -> ClassifyResponse:
        if len(body.ingredients) > MAX_INGREDIENTS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=TOO_MANY_INGREDIENTS)
        if len(body.known_foods) > MAX_KNOWN_FOODS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=TOO_MANY_KNOWN_FOODS)
        keys = [i.key for i in body.ingredients]
        if len(set(keys)) != len(keys):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=DUPLICATE_KEYS)
        if not body.ingredients:
            # Nothing to label; answering without a model call keeps an empty
            # Generate free and instant.
            return ClassifyResponse(items=[])
        try:
            client = get_client()
        except RuntimeError as exc:
            log.warning("Shopping classify: no client (missing key?): %s", exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED) from exc
        try:
            with usage.attributed(user.get("sub", ""), "classify", get_client=get_service_client):
                items = classify_ingredients(client, body.ingredients, body.known_foods)
        except ClassifyCheckError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        except httpx.TimeoutException as exc:
            log.warning("Shopping classify timed out: %s", exc)
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail=CLASSIFY_TIMEOUT) from exc
        except genai_errors.APIError as exc:
            busy = getattr(exc, "code", None) == 429
            log.warning("Gemini refused a shopping classify call (busy=%s): %s", busy, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=CLASSIFY_BUSY if busy else CLASSIFY_UNAVAILABLE,
            ) from exc
        except Exception as exc:
            log.warning("Shopping classify failed unexpectedly: %s", exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=CLASSIFY_UNAVAILABLE) from exc
        return ClassifyResponse(items=items)

    # POST /shopping/aisles - a typed item's aisle, asked in the background after a
    # typed Add (Cayenne "typed items get a section"). One Decisions request per call.
    @router.post("/shopping/aisles", response_model=AislesResponse, status_code=200)
    def aisles(
        body: AislesRequest,
        user: dict = Depends(verify_jwt),
        _gate: None = Depends(require_ai),
    ) -> AislesResponse:
        if not body.foods:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=NO_FOODS)
        if len(body.foods) > MAX_AISLE_FOODS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=TOO_MANY_FOODS)
        if any(not f.strip() or len(f) > MAX_FOOD_CHARS for f in body.foods):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=BAD_FOOD)
        if len(set(body.foods)) != len(body.foods):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=DUPLICATE_FOODS)
        try:
            client = get_decisions_client()
        except RuntimeError as exc:
            log.warning("Shopping aisles: no client (missing key?): %s", exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=AISLES_NOT_CONFIGURED) from exc
        try:
            # Attributed as action "classify", step "aisles": ai_usage's action check
            # (Cayenne migration 20261110120000) admits no "aisles", and a refused
            # insert is swallowed by usage.flush, so a new action would lose the rows.
            with usage.attributed(user.get("sub", ""), "classify", get_client=get_service_client):
                items = decide_aisles_splitting(client, body.foods)
        except AislesCheckError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        except httpx.TimeoutException as exc:
            log.warning("Shopping aisles timed out: %s", exc)
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail=AISLES_TIMEOUT) from exc
        except DecisionsAPIError as exc:
            busy = exc.code == 429
            log.warning("Gemini refused a shopping aisles call (status=%s): %s", exc.code, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=AISLES_BUSY if busy else AISLES_UNAVAILABLE,
            ) from exc
        except Exception as exc:
            log.warning("Shopping aisles failed unexpectedly: %s", exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=AISLES_UNAVAILABLE) from exc
        return AislesResponse(items=items)

    return router
