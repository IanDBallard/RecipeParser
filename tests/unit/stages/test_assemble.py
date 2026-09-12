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
