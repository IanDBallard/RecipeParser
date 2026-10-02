"""
recipeparser/adapters/shopping_api.py — the shopping classify endpoint
(Cayenne's shopping list design, Part 3 §2).

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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from recipeparser.shopping import (
    ClassifyCheckError,
    ClassifiedItem,
    ClassifyIngredient,
    classify_ingredients,
)

log = logging.getLogger(__name__)

MAX_INGREDIENTS = 400
MAX_KNOWN_FOODS = 2000

TOO_MANY_INGREDIENTS = "Too many ingredients for one Generate."
TOO_MANY_KNOWN_FOODS = "Too many known foods for one Generate."
DUPLICATE_KEYS = "Two ingredients carry the same key."


class ClassifyRequest(BaseModel):
    ingredients: List[ClassifyIngredient]
    known_foods: List[str]


class ClassifyResponse(BaseModel):
    items: List[ClassifiedItem]


def build_router(verify_jwt: Callable, get_client: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    # A plain def: FastAPI runs it on the thread pool, and the Gemini call is
    # synchronous (as /embed's is).
    @router.post("/shopping/classify", response_model=ClassifyResponse, status_code=200)
    def classify(body: ClassifyRequest, user: dict = Depends(verify_jwt)) -> ClassifyResponse:
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
            items = classify_ingredients(get_client(), body.ingredients, body.known_foods)
        except ClassifyCheckError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return ClassifyResponse(items=items)

    return router
