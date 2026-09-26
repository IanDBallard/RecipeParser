"""Tests for Cayenne-specific Gemini functions (embeddings, refinement)."""
import pytest
from unittest.mock import MagicMock
from recipeparser.config import GEMINI_MODEL
from recipeparser.gemini import get_embeddings, refine_recipe_for_cayenne
from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection

def test_get_embeddings_success():
    from google.genai import types as genai_types
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_values = [0.1] * 1536
    mock_response.embeddings = [MagicMock(values=mock_values)]
    mock_client.models.embed_content.return_value = mock_response

    result = get_embeddings("test text", mock_client)

    assert result == mock_values
    call_kwargs = mock_client.models.embed_content.call_args.kwargs
    assert call_kwargs["model"] == "models/gemini-embedding-001"
    assert call_kwargs["contents"] == "test text"
    assert call_kwargs["config"].output_dimensionality == 1536


def test_get_embeddings_failure_raises():
    """get_embeddings raises on API failure — callers (the endpoint) handle the 500."""
    mock_client = MagicMock()
    mock_client.models.embed_content.side_effect = Exception("API Error")

    with pytest.raises(Exception, match="API Error"):
        get_embeddings("test text", mock_client)

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
                fallback_string="1 cup flour",
            )
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Use {{ing_01|flour}}."),
        ],
    )

    mock_response = MagicMock()
    # refine uses response_json_schema; we parse response.text manually
    mock_response.text = expected_refined.model_dump_json()
    mock_client.models.generate_content.return_value = mock_response

    raw_recipe = MagicMock()
    raw_recipe.__str__.return_value = "Raw Recipe Text"

    result = refine_recipe_for_cayenne(raw_recipe, mock_client)

    assert result is not None
    assert result.title == expected_refined.title
    assert result.base_servings == expected_refined.base_servings
    args, kwargs = mock_client.models.generate_content.call_args
    assert kwargs["model"] == GEMINI_MODEL
    assert "response_json_schema" in kwargs["config"]
    assert "additionalProperties" not in str(kwargs["config"]["response_json_schema"])
    assert "Raw Recipe Text" in kwargs["contents"]

def test_refine_recipe_for_cayenne_failure_returns_none():
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("Refinement failed")

    result = refine_recipe_for_cayenne("raw text", mock_client)
    assert result is None


from recipeparser.gemini import build_refine_prompt
from recipeparser.models import RecipeExtraction, StructuredIngredient


def test_refine_prompt_states_liquid_or_solid_before_it_converts():
    # Cookbook locales D13: the state is a prerequisite of the conversion, so it is asked first.
    raw = RecipeExtraction(
        name="Cake", photo_filename="cake.jpg", servings="4", prep_time="5 mins", cook_time="30 mins",
        ingredients=["1 cup milk", "2 cups flour"], directions=["Mix."],
    )
    prompt = build_refine_prompt(raw, None)
    state_at = prompt.index("STATE:")
    conversion_at = prompt.index("CONVERSION:")
    assert 0 < state_at < conversion_at
    assert '"liquid"' in prompt and '"solid"' in prompt
    assert "Use the state when you compute the conversion" in prompt


def test_refine_prompt_carries_the_host_and_no_reader_context():
    raw = RecipeExtraction(name="Cake", ingredients=["1 cup milk"], directions=["Mix."])
    prompt = build_refine_prompt(raw, "taste.com.au")
    assert "SOURCE HOST: taste.com.au" in prompt
    assert prompt.index("SOURCE HOST:") < prompt.index("RAW RECIPE:")
    assert "UOM System" not in prompt and "Measure Preference" not in prompt
    assert "DUAL MEASURES:" in prompt and "SOURCE SYSTEM:" in prompt
    assert "SOURCE HOST: none" in build_refine_prompt(raw, None)


def test_the_rebuilt_refinement_keeps_the_detection(monkeypatch):
    # gemini.py rebuilds the refinement when axes are present; the detection must survive it.
    expected = CayenneRefinement(
        title="Cake", base_servings=4,
        structured_ingredients=[StructuredIngredient(id="ing_01", amount=1.0, unit="cup", name="milk", fallback_string="1 cup (250 ml) milk")],
        tokenized_directions=[TokenizedDirection(step=1, text="Use {{ing_01|milk}}.")],
        source_uom_system_detected="AU", source_uom_system_evidence="1 cup (250 ml)",
    )
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=expected.model_dump_json())
    result = refine_recipe_for_cayenne("raw", client, user_axes={"Cuisine": ["Italian"]})
    assert (result.source_uom_system_detected, result.source_uom_system_evidence) == ("AU", "1 cup (250 ml)")


def test_structured_ingredient_carries_an_optional_state():
    ing = StructuredIngredient(id="ing_01", name="milk", fallback_string="1 cup milk", state="liquid")
    assert ing.state == "liquid"
    assert StructuredIngredient(id="ing_02", name="flour", fallback_string="2 cups flour").state is None
    with pytest.raises(ValueError):
        StructuredIngredient(id="ing_03", name="x", fallback_string="x", state="wet")


def test_refine_prompt_sizes_the_imperial_measures_and_names_their_evidence():
    """Imperial measures D5: REFINE's conversions use Cayenne's sizes, and the pre-metric British
    measures are evidence of Imperial, the dessertspoon excepted."""
    raw = RecipeExtraction(name="Syllabub", ingredients=["1 gill cream"], directions=["Whip."])
    prompt = build_refine_prompt(raw, None)
    assert "gill = 142 ml (a US gill = 118 ml); teacup = 142 ml; breakfast cup = 227 ml; dessertspoon = 10 ml; stone = 14 lb; dram = 1/16 oz (a weight)" in prompt
    assert '"breakfast cup", "teacup" or "stone" as a measure, or "gill" when nothing points to the US, is Imperial' in prompt
    assert '"dessertspoon" alone is not evidence' in prompt
    assert prompt.index("SOURCE SYSTEM:") < prompt.index("RAW RECIPE:")
