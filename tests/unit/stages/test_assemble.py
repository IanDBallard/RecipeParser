"""tests/unit/stages/test_assemble.py — ASSEMBLE resolves the citation into the four columns."""
from recipeparser.core.citation import book_citation, web_citation
from recipeparser.core.models import SourceMeta
from recipeparser.core.stages.assemble import assemble
from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection


def _refined() -> CayenneRefinement:
    return CayenneRefinement(
        title="Cake",
        base_servings=2,
        structured_ingredients=[
            StructuredIngredient(id="ing_01", name="flour", fallback_string="1 cup flour", line_index=0)
        ],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
    )


def _assemble(**kwargs):
    return assemble(
        recipe=_refined(), embedding=[0.0] * 1536, source_url=None, image_url=None, grid_categories={}, **kwargs
    )


def test_assemble_writes_the_four_columns_and_the_display_source():
    r = _assemble(citation=book_citation("Italian Food", "Elizabeth David"))
    assert (r.source_kind, r.source_key, r.source_title, r.source_author) == (
        "book", "italian food", "Italian Food", "Elizabeth David")
    assert r.source == "Italian Food — Elizabeth David"


def test_assemble_without_a_citation_leaves_all_five_null():
    r = _assemble()
    assert (r.source_kind, r.source_key, r.source_title, r.source_author, r.source) == (None, None, None, None, None)


def test_assemble_unknown_book_has_a_key_but_no_display():
    r = _assemble(citation=book_citation(None, None))
    assert (r.source_kind, r.source_key, r.source_title, r.source) == ("unknown", "unknown-book", None, None)


def test_paprika_source_beats_the_citation_display():
    meta = SourceMeta(source="NYT Cooking")
    r = _assemble(citation=web_citation("https://cooking.nytimes.com/r/1"), meta=meta)
    assert r.source == "NYT Cooking"
    assert r.source_key == "cooking.nytimes.com"


def test_a_stated_total_fills_the_cook_span_when_cook_is_absent():
    r = _assemble(total_time="8 to 10 hours, plus refrigeration")
    assert (r.cook_min_minutes, r.cook_max_minutes, r.cook_note) == (480, 600, "plus refrigeration")
    assert r.cook_time is None            # the text column is what the page said for cook: nothing


def test_a_stated_cook_time_beats_the_total():
    r = _assemble(cook_time="30 mins", total_time="8 hours")
    assert (r.cook_min_minutes, r.cook_max_minutes) == (30, 30)
    assert r.cook_time == "30 mins"


def test_a_paprika_cook_time_still_beats_both():
    r = _assemble(cook_time="30 mins", total_time="8 hours", meta=SourceMeta(cook_time="45 mins"))
    assert (r.cook_min_minutes, r.cook_max_minutes, r.cook_time) == (45, 45, "45 mins")


def test_the_extracted_description_and_nutrition_are_kept_when_no_source_states_them():
    r = _assemble(description="A weeknight noodle dish.", nutritional_info="572 calories; 19 grams fat")
    assert r.description == "A weeknight noodle dish."
    assert r.nutritional_info == "572 calories; 19 grams fat"


def test_a_source_statement_beats_the_extracted_description_and_nutrition():
    meta = SourceMeta(description="The page's own blurb.", nutritional_info="Per serving: 600 kcal")
    r = _assemble(description="model's blurb", nutritional_info="model's nutrition", meta=meta)
    assert r.description == "The page's own blurb."
    assert r.nutritional_info == "Per serving: 600 kcal"


def test_a_source_that_states_only_one_of_them_does_not_blank_the_other():
    meta = SourceMeta(description="The page's own blurb.")
    r = _assemble(description="model's blurb", nutritional_info="572 calories", meta=meta)
    assert r.description == "The page's own blurb."
    assert r.nutritional_info == "572 calories"


def test_nothing_stated_anywhere_stays_null():
    r = _assemble()
    assert (r.description, r.nutritional_info, r.cook_min_minutes) == (None, None, None)


def test_the_full_pipeline_hands_assemble_the_three_extracted_fields(monkeypatch):
    """The call site in RecipePipeline._process_chunk passes total_time, description and nutritional_info."""
    import inspect

    from recipeparser.core import pipeline as pipeline_mod

    src = inspect.getsource(pipeline_mod.RecipePipeline._process_chunk)
    for name in ("total_time", "description", "nutritional_info"):
        assert f'{name}=getattr(raw, "{name}", None)' in src, name
