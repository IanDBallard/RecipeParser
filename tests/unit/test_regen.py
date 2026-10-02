"""Pure regen helpers (spec 5.2, 5.7)."""
import pytest

from recipeparser.core.regen import build_extraction, build_update, raw_lines_from_derived, strip_fat_tokens
from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection


def test_strip_fat_tokens_replaces_with_fallback():
    assert strip_fat_tokens("Mix {{ing_01|1.5 cups flour}} into {{ing_02|the eggs}}.") == \
        "Mix 1.5 cups flour into the eggs."


def test_strip_fat_tokens_leaves_plain_text():
    assert strip_fat_tokens("Bake for 20 minutes.") == "Bake for 20 minutes."


def test_raw_lines_from_derived():
    structured = [
        StructuredIngredient(id="ing_01", name="flour", fallback_string="1 1/2 cups flour"),
        StructuredIngredient(id="ing_02", name="egg", fallback_string="2 eggs"),
    ]
    tokenized = [
        TokenizedDirection(step=1, text="Mix {{ing_01|flour}} and {{ing_02|eggs}}."),
        TokenizedDirection(step=2, text="Bake."),
    ]
    lines, steps = raw_lines_from_derived(structured, tokenized)
    assert lines == ["1 1/2 cups flour", "2 eggs"]
    assert steps == ["Mix flour and eggs.", "Bake."]


def test_build_extraction_from_row():
    row = {"title": "Cake", "ingredient_lines": ["1 cup flour", "2 eggs"], "direction_steps": ["Mix."]}
    ex = build_extraction(row)
    assert ex.name == "Cake"
    assert ex.ingredients == ["1 cup flour", "2 eggs"]
    assert ex.directions == ["Mix."]
    assert ex.servings is None


def test_build_extraction_keeps_empty_lists():
    # An empty list is a real body: the cook deleted every line. Regenerating it
    # to empty derived data is correct, unlike a null (below).
    ex = build_extraction({"title": "Cake", "ingredient_lines": [], "direction_steps": []})
    assert ex.ingredients == [] and ex.directions == []


@pytest.mark.parametrize("column", ["ingredient_lines", "direction_steps"])
def test_build_extraction_rejects_a_null_body_column(column):
    # Fix Roadmap F-005. Null used to read as [], so REFINE got nothing, succeeded,
    # and the worker wrote empty derived data back as fresh: the recipe's
    # ingredients (or method) silently vanished. The columns are NOT NULL
    # default '[]' (Cayenne migration 013), so a null is a row this code does not
    # understand; it must fail and be recorded, not regenerate to nothing.
    row = {"title": "Cake", "ingredient_lines": ["1 cup flour"], "direction_steps": ["Mix."]}
    row[column] = None
    with pytest.raises(ValueError, match=f"{column} is null"):
        build_extraction(row)


def test_build_extraction_rejects_a_missing_body_column():
    # A claim result without the column is the same hazard, for every row at once.
    with pytest.raises(ValueError, match="ingredient_lines is null"):
        build_extraction({"title": "Cake", "direction_steps": ["Mix."]})


def test_build_extraction_rejects_a_double_encoded_ingredient_column():
    # A double-encoded jsonb column arrives as a str. Iterating it yields
    # characters, so REFINE would get one "ingredient" per character, succeed,
    # and the worker would write the garbage back under the body_rev guard with
    # no error and no attempt counted. Fail loudly instead: the worker records
    # it via regen_failed and the attempt cap stops the row.
    row = {"title": "Cake", "ingredient_lines": '["1 cup flour", "2 eggs"]', "direction_steps": []}
    with pytest.raises(TypeError, match="ingredient_lines must be a list, got str"):
        build_extraction(row)


def test_build_extraction_rejects_a_double_encoded_direction_column():
    row = {"title": "Cake", "ingredient_lines": [], "direction_steps": '["Mix."]'}
    with pytest.raises(TypeError, match="direction_steps must be a list, got str"):
        build_extraction(row)


def test_build_update_payload():
    ref = CayenneRefinement(
        title="Cake", base_servings=99,
        structured_ingredients=[StructuredIngredient(id="ing_01", name="flour",
                                                     fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
        grid_categories={"Cuisine": ["Italian"]},
    )
    payload = build_update(ref, [0.1] * 3, read_rev=7)
    assert payload == {
        "structured_ingredients": [ref.structured_ingredients[0].model_dump()],
        "tokenized_directions": [{"step": 1, "text": "Mix {{ing_01|flour}}."}],
        "embedding": [0.1] * 3,
        "derived_rev": 7,
        "amount_overrides": {},
        "derived_error": None,
        "derived_attempts": 0,
        "claimed_at": None,
        "source_uom_system_detected": None,
        "source_uom_system_evidence": None,
    }
    # base_servings, grid_categories and title are user-owned after ingest (D3, 3.6)
    assert "base_servings" not in payload and "title" not in payload


def test_strip_fat_tokens_drops_the_use_field():
    assert strip_fat_tokens("Add about {{ing_02|1/2 cup (60 g)|0.5 cup}} flour; add more {{ing_02|flour|none}}.") == \
        "Add about 1/2 cup (60 g) flour; add more flour."
