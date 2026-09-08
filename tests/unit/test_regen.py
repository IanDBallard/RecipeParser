"""Pure regen helpers (spec 5.2, 5.7)."""
from recipeparser.core.regen import raw_lines_from_derived, strip_fat_tokens
from recipeparser.models import StructuredIngredient, TokenizedDirection


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
