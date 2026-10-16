"""tests/unit/stages/test_assemble.py — ASSEMBLE resolves the citation into the four columns."""
from recipeparser.core.citation import book_citation, web_citation
from recipeparser.core.models import SourceMeta
from recipeparser.core.stages.assemble import assemble
from recipeparser.models import CayenneRefinement, RecipePan, StructuredIngredient, TokenizedDirection


def _refined() -> CayenneRefinement:
    return CayenneRefinement(
        title="Cake",
        base_servings=2,
        structured_ingredients=[
            StructuredIngredient(id="ing_01", kind="ingredient", name="flour", fallback_string="1 cup flour", line_index=0)
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


def test_the_extracted_notes_are_kept_when_no_source_states_them():
    r = _assemble(notes="Best the next day.")
    assert r.notes == "Best the next day."


def test_a_source_statement_beats_the_extracted_notes():
    r = _assemble(notes="model's note", meta=SourceMeta(notes="The cook's own note."))
    assert r.notes == "The cook's own note."


def test_a_source_with_no_notes_does_not_blank_the_extracted_ones():
    r = _assemble(notes="Best the next day.", meta=SourceMeta(description="A blurb."))
    assert r.notes == "Best the next day."


def test_the_full_pipeline_hands_assemble_the_four_extracted_fields(monkeypatch):
    """The call site in RecipePipeline._process_chunk passes total_time, description, nutritional_info and notes."""
    import inspect

    from recipeparser.core import pipeline as pipeline_mod

    src = inspect.getsource(pipeline_mod.RecipePipeline._process_chunk)
    for name in ("total_time", "description", "nutritional_info", "notes"):
        assert f'{name}=getattr(raw, "{name}", None)' in src, name


def test_assemble_carries_the_tins_bake_type_and_bake_step():
    refined = _refined()
    refined.pans = [RecipePan(id="pan_01", raw="a 20 cm tin", shape="round", family="round-cake")]
    refined.bake_type = "sponge"
    refined.bake_step = 0
    r = assemble(recipe=refined, embedding=[0.0] * 1536, source_url=None, image_url=None, grid_categories={})
    assert r.pans == refined.pans
    assert (r.bake_type, r.bake_step) == ("sponge", 0)


# --- Cayenne restore (plan B task 3) -----------------------------------------

_SPANS = {
    "servings_min": 10, "servings_max": 12, "servings_note": "cakes",
    "prep_min_minutes": 30, "prep_max_minutes": 45, "prep_note": "overnight",
    "cook_min_minutes": 60, "cook_max_minutes": 90, "cook_note": "slow",
    "base_servings": 10,
}


def test_assemble_uses_given_spans():
    r = _assemble(spans=_SPANS, servings_text=None, prep_time="1 hr", cook_time="2 hr")
    for key, value in _SPANS.items():
        assert getattr(r, key) == value, key


def test_assemble_carries_restore_fields():
    from recipeparser.core.models import RestoreFields

    r = _assemble(restore=RestoreFields(is_favourite=True, source_uom_system="UK", image_source="generated", stale=True))
    assert (r.is_favourite, r.source_uom_system, r.image_source, r.stale) == (True, "UK", "generated", True)
