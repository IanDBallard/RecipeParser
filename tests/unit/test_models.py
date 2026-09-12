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
