"""Tests for the Cayenne Ingestion API (/ingest endpoint).

Coverage:
  1. Input validation (missing/empty/URL-only)
  2. Happy path — full pipeline success, response schema validation
  3. Pipeline error branches (no recipes, refinement failure, embedding failure)
  4. Missing API key → 500
  5. Unexpected exception → 500
  6. UOM / measure_preference passthrough to refine_recipe_for_cayenne
  7. Fat Token format preserved in tokenized_directions
  8. AI-converted ingredient fields surfaced correctly
  9. prep_time / cook_time sourced from raw recipe
  10. base_servings fallback (None → 4)
"""
import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")

from recipeparser.api import app
from recipeparser.models import (
    RecipeExtraction,
    RecipeList,
    StructuredIngredient,
    TokenizedDirection,
    CayenneRefinement,
)

client = TestClient(app)

# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

def _make_raw_recipe(**kwargs):
    defaults = dict(
        name="Test Cake",
        ingredients=["1 cup flour", "2 eggs"],
        directions=["Mix well.", "Bake 30 mins."],
        prep_time="10 mins",
        cook_time="30 mins",
        servings="4",
    )
    defaults.update(kwargs)
    return RecipeExtraction(**defaults)


def _make_refined(**kwargs):
    defaults = dict(
        title="Test Cake",
        base_servings=4,
        structured_ingredients=[
            StructuredIngredient(
                id="ing_01",
                amount=1.0,
                unit="cup",
                name="flour",
                fallback_string="1 cup flour",
                converted_amount=None,
                converted_unit=None,
                is_ai_converted=False,
            ),
            StructuredIngredient(
                id="ing_02",
                amount=2.0,
                unit=None,
                name="eggs",
                fallback_string="2 eggs",
                converted_amount=None,
                converted_unit=None,
                is_ai_converted=False,
            ),
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Mix {{ing_01|1 cup flour}} well."),
            TokenizedDirection(step=2, text="Bake 30 mins."),
        ],
    )
    defaults.update(kwargs)
    return CayenneRefinement(**defaults)


FAKE_EMBEDDING = [0.1] * 1536

MOCK_TARGETS = {
    "extract": "recipeparser.gemini.extract_recipes",
    "refine": "recipeparser.gemini.refine_recipe_for_cayenne",
    "embed": "recipeparser.gemini.get_embeddings",
    "client": "recipeparser.api._get_client",
}


# ---------------------------------------------------------------------------
# 1. Input validation
# ---------------------------------------------------------------------------

def test_ingest_missing_both_url_and_text():
    resp = client.post("/ingest", json={})
    assert resp.status_code == 400
    assert "Only text ingestion" in resp.json()["detail"]


def test_ingest_url_only_not_yet_supported():
    resp = client.post("/ingest", json={"url": "https://example.com/recipe"})
    assert resp.status_code == 400


def test_ingest_empty_text_rejected():
    resp = client.post("/ingest", json={"text": ""})
    assert resp.status_code == 400


def test_ingest_whitespace_text_rejected():
    resp = client.post("/ingest", json={"text": "   "})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 2. Happy path — full pipeline success
# ---------------------------------------------------------------------------

def test_ingest_happy_path_returns_200():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 200


def test_ingest_happy_path_response_schema():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    # Top-level required keys
    for key in ("title", "prep_time", "cook_time", "base_servings",
                "source_url", "categories", "structured_ingredients",
                "tokenized_directions", "embedding"):
        assert key in data, f"Missing key: {key}"


def test_ingest_happy_path_embedding_length():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert len(resp.json()["embedding"]) == 1536


def test_ingest_happy_path_ingredient_schema():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    ing = resp.json()["structured_ingredients"][0]
    for key in ("id", "amount", "unit", "name", "fallback_string",
                "converted_amount", "converted_unit", "is_ai_converted"):
        assert key in ing, f"Ingredient missing key: {key}"


def test_ingest_happy_path_direction_schema():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]) as mock_client, \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    direction = resp.json()["tokenized_directions"][0]
    assert "step" in direction
    assert "text" in direction


# ---------------------------------------------------------------------------
# 3. Pipeline error branches
# ---------------------------------------------------------------------------

def test_ingest_no_recipes_found_returns_422():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[])), \
         patch(MOCK_TARGETS["refine"]), \
         patch(MOCK_TARGETS["embed"]):
        resp = client.post("/ingest", json={"text": "Not a recipe"})
    assert resp.status_code == 422
    assert "No recipes found" in resp.json()["detail"]


def test_ingest_extract_returns_none_returns_422():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=None), \
         patch(MOCK_TARGETS["refine"]), \
         patch(MOCK_TARGETS["embed"]):
        resp = client.post("/ingest", json={"text": "Not a recipe"})
    assert resp.status_code == 422


def test_ingest_refinement_returns_none_returns_500():
    raw = _make_raw_recipe()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=None), \
         patch(MOCK_TARGETS["embed"]):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "Refinement pass failed" in resp.json()["detail"]


def test_ingest_embedding_raises_returns_500():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], side_effect=RuntimeError("embed failed")):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# 4. Missing API key → 500
# ---------------------------------------------------------------------------

def test_ingest_missing_api_key_returns_500():
    with patch("recipeparser.api._get_client", side_effect=RuntimeError("GOOGLE_API_KEY not found")):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "GOOGLE_API_KEY" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Unexpected exception → 500
# ---------------------------------------------------------------------------

def test_ingest_unexpected_exception_returns_500():
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], side_effect=Exception("boom")):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.status_code == 500
    assert "boom" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 6. UOM / measure_preference passthrough
# ---------------------------------------------------------------------------

def test_ingest_uom_passthrough_to_refine():
    raw = _make_raw_recipe()
    refined = _make_refined()
    mock_refine = MagicMock(return_value=refined)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], mock_refine), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        client.post("/ingest", json={
            "text": "Some recipe text",
            "uom_system": "Metric",
            "measure_preference": "Weight",
        })
    mock_refine.assert_called_once()
    _, kwargs = mock_refine.call_args
    assert kwargs.get("uom_system") == "Metric"
    assert kwargs.get("measure_preference") == "Weight"


def test_ingest_default_uom_is_us_volume():
    raw = _make_raw_recipe()
    refined = _make_refined()
    mock_refine = MagicMock(return_value=refined)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], mock_refine), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        client.post("/ingest", json={"text": "Some recipe text"})
    _, kwargs = mock_refine.call_args
    assert kwargs.get("uom_system") == "US"
    assert kwargs.get("measure_preference") == "Volume"


# ---------------------------------------------------------------------------
# 7. Fat Token format preserved in tokenized_directions
# ---------------------------------------------------------------------------

def test_ingest_fat_token_format_preserved():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    directions = resp.json()["tokenized_directions"]
    step1_text = directions[0]["text"]
    assert "{{ing_01|1 cup flour}}" in step1_text


def test_ingest_direction_steps_are_1_based():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    steps = [d["step"] for d in resp.json()["tokenized_directions"]]
    assert steps[0] == 1


# ---------------------------------------------------------------------------
# 8. AI-converted ingredient fields
# ---------------------------------------------------------------------------

def test_ingest_ai_converted_ingredient_fields():
    raw = _make_raw_recipe()
    refined = _make_refined(
        structured_ingredients=[
            StructuredIngredient(
                id="ing_01",
                amount=1.0,
                unit="cup",
                name="flour",
                fallback_string="1 cup flour",
                converted_amount=120.0,
                converted_unit="g",
                is_ai_converted=True,
            ),
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Mix {{ing_01|1 cup flour}}."),
        ],
    )
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    ing = resp.json()["structured_ingredients"][0]
    assert ing["is_ai_converted"] is True
    assert ing["converted_amount"] == 120.0
    assert ing["converted_unit"] == "g"


# ---------------------------------------------------------------------------
# 9. prep_time / cook_time sourced from raw recipe
# ---------------------------------------------------------------------------

def test_ingest_times_come_from_raw_recipe():
    raw = _make_raw_recipe(prep_time="15 mins", cook_time="45 mins")
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    assert data["prep_time"] == "15 mins"
    assert data["cook_time"] == "45 mins"


def test_ingest_times_none_when_raw_has_none():
    raw = _make_raw_recipe(prep_time=None, cook_time=None)
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    data = resp.json()
    assert data["prep_time"] is None
    assert data["cook_time"] is None


# ---------------------------------------------------------------------------
# 10. base_servings fallback (None → 4)
# ---------------------------------------------------------------------------

def test_ingest_base_servings_fallback_to_4():
    raw = _make_raw_recipe()
    refined = _make_refined(base_servings=None)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.json()["base_servings"] == 4


def test_ingest_base_servings_uses_refined_value():
    raw = _make_raw_recipe()
    refined = _make_refined(base_servings=6)
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.json()["base_servings"] == 6


# ---------------------------------------------------------------------------
# 11. source_url echoed back from request
# ---------------------------------------------------------------------------

def test_ingest_source_url_echoed_when_text_provided():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={
            "text": "Some recipe text",
            "url": "https://example.com/my-recipe",
        })
    assert resp.json()["source_url"] == "https://example.com/my-recipe"


def test_ingest_source_url_is_none_when_not_provided():
    raw = _make_raw_recipe()
    refined = _make_refined()
    with patch(MOCK_TARGETS["client"]), \
         patch(MOCK_TARGETS["extract"], return_value=RecipeList(recipes=[raw])), \
         patch(MOCK_TARGETS["refine"], return_value=refined), \
         patch(MOCK_TARGETS["embed"], return_value=FAKE_EMBEDDING):
        resp = client.post("/ingest", json={"text": "Some recipe text"})
    assert resp.json()["source_url"] is None
