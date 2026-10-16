"""SourceMeta's normalisation rule (design §5.1) and Chunk's new field."""
from __future__ import annotations

from recipeparser.core.models import Chunk, InputType, SourceMeta


class TestSourceMeta:
    def test_defaults_are_all_none(self):
        meta = SourceMeta()
        assert meta.prep_time is None
        assert meta.cook_time is None
        assert meta.source is None
        assert meta.notes is None
        assert meta.rating is None
        assert meta.nutritional_info is None
        assert meta.description is None
        assert meta.difficulty is None

    def test_text_is_stripped(self):
        meta = SourceMeta(prep_time="  20 min  ", source="\tBon Appétit\n")
        assert meta.prep_time == "20 min"
        assert meta.source == "Bon Appétit"

    def test_an_empty_string_becomes_none(self):
        # Paprika writes "" for a field the recipe never filled in.
        meta = SourceMeta(cook_time="", notes="   ", difficulty="")
        assert meta.cook_time is None
        assert meta.notes is None
        assert meta.difficulty is None

    def test_a_rating_of_zero_becomes_none(self):
        # Paprika stores 0 for unrated; the column must mean unrated, not zero stars.
        assert SourceMeta(rating=0).rating is None

    def test_a_real_rating_survives(self):
        assert SourceMeta(rating=4).rating == 4

    def test_a_non_integer_rating_becomes_none(self):
        assert SourceMeta(rating="").rating is None
        assert SourceMeta(rating="four").rating is None

    def test_a_numeric_string_rating_is_read_as_a_number(self):
        assert SourceMeta(rating="3").rating == 3


class TestChunkMeta:
    def test_meta_defaults_to_none(self):
        assert Chunk(text="x", input_type=InputType.PDF).meta is None

    def test_meta_round_trips(self):
        meta = SourceMeta(prep_time="10 min")
        chunk = Chunk(text="x", input_type=InputType.PAPRIKA_LEGACY, meta=meta)
        assert chunk.meta is meta


def test_extraction_asks_for_a_stated_source_and_a_byline():
    from recipeparser.models import RecipeExtraction

    schema = RecipeExtraction.model_json_schema()
    assert "stated_source" in schema["properties"]
    assert "byline" in schema["properties"]
    # The instruction the prompt defect needs: the model must never name itself.
    assert "Never the name of an AI model" in schema["properties"]["stated_source"]["description"]
    r = RecipeExtraction(name="x", ingredients=[], directions=[])
    assert r.stated_source is None and r.byline is None


def test_extraction_source_fields_stay_out_of_repr():
    # tests/goldens/golden_client.py keys recorded refine replies by the sha of
    # str(raw_recipe); these two fields are repr=False so adding them did not
    # move a single recording. Removing repr=False costs a paid re-record.
    from recipeparser.models import RecipeExtraction

    text = str(RecipeExtraction(
        name="x", ingredients=[], directions=[],
        stated_source="NYT Cooking", byline="Melissa Clark",
    ))
    assert "stated_source" not in text and "byline" not in text
    assert "NYT Cooking" not in text


def test_extraction_asks_for_a_total_time_a_description_and_nutrition():
    from recipeparser.models import RecipeExtraction

    props = RecipeExtraction.model_json_schema()["properties"]
    assert "total_time" in props and "description" in props and "nutritional_info" in props
    # The one instruction the pulled-pork ingest needed: a stated total is never invented.
    assert "Never the sum" in props["total_time"]["description"]
    assert "verbatim" in props["nutritional_info"]["description"]
    r = RecipeExtraction(name="x", ingredients=[], directions=[])
    assert (r.total_time, r.description, r.nutritional_info) == (None, None, None)


def test_the_three_new_extraction_fields_stay_out_of_repr():
    # Same invariant as stated_source/byline: the refine prompt's body is
    # str(raw_recipe), and the golden recordings are keyed by it.
    from recipeparser.models import RecipeExtraction

    text = str(RecipeExtraction(
        name="x", ingredients=[], directions=[],
        total_time="8 to 10 hours", description="A weeknight dish.", nutritional_info="572 calories",
    ))
    for needle in ("total_time", "description", "nutritional_info", "8 to 10 hours", "weeknight", "572"):
        assert needle not in text


def test_both_extract_prompts_name_the_three_fields():
    from recipeparser import gemini

    for prompt in (gemini.build_plain_text_prompt("x"), gemini.build_extract_prompt("x")):
        assert "total_time:" in prompt and "description:" in prompt and "nutritional_info:" in prompt


from recipeparser.models import CayenneRecipe, CayenneRefinement, PanAnswer, RecipePan  # noqa: E402


def test_refinement_defaults_to_no_tins_and_no_bake():
    r = CayenneRefinement(title="T", base_servings=4, structured_ingredients=[], tokenized_directions=[])
    assert r.pans == [] and r.bake_type is None and r.bake_step is None


def test_recipe_pan_round_trips_with_mm_and_hint():
    p = RecipePan.model_validate({"id": "pan_01", "raw": "two 20cm tins", "count": 2, "shape": "round",
                                  "family": "round-cake", "dims_mm": {"d": 200}, "capacity_ml": None,
                                  "catalog_hint": None})
    assert p.dims_mm.d == 200 and p.edited is False and p.model_dump()["dims_mm"]["depth"] is None


def test_pan_answer_tolerates_free_text_vocab():
    a = PanAnswer.model_validate({"pans": [{"id": "pan_01", "raw": "a tin", "count": 1, "shape": "blob",
                                            "family": "cake", "dims_mm": {}}], "bake_type": "souffle",
                                  "bake_step": 9, "mentions": [], "core": []})
    assert a.pans[0].shape == "blob"  # vocab is enforced by core/pans.py, never by validation


def _property_names(obj):
    """Every property name anywhere in a JSON schema."""
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "properties" and isinstance(v, dict):
                found.update(v)
            found |= _property_names(v)
    elif isinstance(obj, list):
        for i in obj:
            found |= _property_names(i)
    return found


def test_refine_and_pan_schemas_have_no_client_fields():
    from recipeparser.gemini import refine_json_schema, schema_without_client_fields
    for schema in (refine_json_schema(), schema_without_client_fields(PanAnswer.model_json_schema())):
        names = _property_names(schema)
        assert "edited" not in names and "removed" not in names
    assert "pans" in refine_json_schema()["properties"]
    # The helper is what strips them: the raw model schema still carries both.
    raw = _property_names(PanAnswer.model_json_schema())
    assert "edited" in raw and "removed" in raw


def test_the_pan_call_schema_has_no_client_fields():
    from unittest.mock import MagicMock, patch

    from recipeparser import gemini
    response = MagicMock()
    response.text = '{"pans": [], "bake_type": null, "bake_step": null, "mentions": [], "core": []}'
    with patch.object(gemini, "_call_with_retry", return_value=response) as call:
        gemini.tag_pans(["Bake."], ["flour"], None, MagicMock())
    names = _property_names(call.call_args.kwargs["config"]["response_json_schema"])
    assert "pans" in names and "edited" not in names and "removed" not in names


_V2 = {
    "title": "T", "structured_ingredients": [], "tokenized_directions": [],
    "is_favourite": True, "category_paths": [["Course", "Dessert"], ["Cuisine"]],
    "source_uom_system": "UK", "image_source": "generated", "stale": True,
}


def test_cayenne_recipe_accepts_v2_fields():
    from recipeparser.models import CayenneRecipe
    r = CayenneRecipe.model_validate(_V2)
    assert r.is_favourite is True
    assert r.category_paths == [["Course", "Dessert"], ["Cuisine"]]
    assert r.source_uom_system == "UK" and r.image_source == "generated" and r.stale is True


def test_cayenne_recipe_v1_meta_still_validates():
    from recipeparser.models import CayenneRecipe
    r = CayenneRecipe.model_validate({"title": "T", "structured_ingredients": [], "tokenized_directions": []})
    assert r.category_paths == [] and r.stale is False and r.is_favourite is None
    assert r.source_uom_system is None and r.image_source is None


def test_recipe_pan_keeps_removed():
    p = RecipePan.model_validate({"id": "pan_01", "raw": "a tin", "removed": True})
    assert p.removed is True
    assert RecipePan.model_validate({"id": "pan_01", "raw": "a tin"}).removed is False


def test_cayenne_recipe_reads_null_paths_and_stale_as_defaults():
    r = CayenneRecipe.model_validate(
        {"title": "T", "structured_ingredients": [], "tokenized_directions": [],
         "category_paths": None, "stale": None}
    )
    assert r.category_paths == []
    assert r.stale is False
