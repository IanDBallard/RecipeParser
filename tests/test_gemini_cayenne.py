"""Tests for Cayenne-specific Gemini functions (embeddings, refinement)."""
import pytest
from unittest.mock import MagicMock
from recipeparser.gemini import get_embeddings, refine_recipe_for_cayenne
from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection

def test_get_embeddings_success():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_values = [0.1] * 1536
    mock_response.embeddings = [MagicMock(values=mock_values)]
    mock_client.models.embed_content.return_value = mock_response

    result = get_embeddings("test text", mock_client)
    
    assert result == mock_values
    mock_client.models.embed_content.assert_called_once_with(
        model="text-embedding-004",
        contents="test text"
    )

def test_get_embeddings_failure_returns_zeros():
    mock_client = MagicMock()
    mock_client.models.embed_content.side_effect = Exception("API Error")

    result = get_embeddings("test text", mock_client)
    
    assert len(result) == 1536
    assert all(v == 0.0 for v in result)

def test_refine_recipe_for_cayenne_success():
    mock_client = MagicMock()
    expected_refined = CayenneRefinement(
        title="Refined Cake",
        base_servings=4,
        structured_ingredients=[
            StructuredIngredient(
                id="ing_01",
                amount=1.0,
                unit="cup",
                name="flour",
                fallback_string="1 cup flour"
            )
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Use {{ing_01|flour}}.")
        ]
    )
    
    mock_response = MagicMock()
    mock_response.parsed = expected_refined
    mock_client.models.generate_content.return_value = mock_response

    raw_recipe = MagicMock()
    raw_recipe.__str__.return_value = "Raw Recipe Text"

    result = refine_recipe_for_cayenne(raw_recipe, mock_client)
    
    assert result == expected_refined
    args, kwargs = mock_client.models.generate_content.call_args
    assert kwargs["model"] == "gemini-2.0-flash"
    assert kwargs["config"]["response_schema"] == CayenneRefinement
    assert "Raw Recipe Text" in kwargs["contents"]

def test_refine_recipe_for_cayenne_failure_returns_none():
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("Refinement failed")

    result = refine_recipe_for_cayenne("raw text", mock_client)
    assert result is None
