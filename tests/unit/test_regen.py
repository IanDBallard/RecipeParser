"""Pure regen helpers (spec 5.2, 5.7)."""
import pytest

from recipeparser.core.regen import build_extraction, build_update, raw_lines_from_derived, strip_fat_tokens
from recipeparser.models import CayenneRefinement, RecipePan, StructuredIngredient, TokenizedDirection


def test_strip_fat_tokens_replaces_with_fallback():
    assert strip_fat_tokens("Mix {{ing_01|1.5 cups flour}} into {{ing_02|the eggs}}.") == \
        "Mix 1.5 cups flour into the eggs."


def test_strip_fat_tokens_leaves_plain_text():
    assert strip_fat_tokens("Bake for 20 minutes.") == "Bake for 20 minutes."


def test_raw_lines_from_derived():
    structured = [
        StructuredIngredient(id="ing_01", kind="ingredient", name="flour", fallback_string="1 1/2 cups flour"),
        StructuredIngredient(id="ing_02", kind="ingredient", name="egg", fallback_string="2 eggs"),
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
        structured_ingredients=[StructuredIngredient(id="ing_01", kind="ingredient", name="flour",
                                                     fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
        grid_categories={"Cuisine": ["Italian"]},
        pans=[RecipePan(id="pan_01", raw="a 20 cm tin")],
        bake_type="sponge",
        bake_step=1,
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
        "pans": [ref.pans[0].model_dump()],
        "bake_type": "sponge",
        "bake_step": 1,
    }
    # base_servings, grid_categories and title are user-owned after ingest (D3, 3.6)
    assert "base_servings" not in payload and "title" not in payload


def test_strip_fat_tokens_drops_the_use_field():
    assert strip_fat_tokens("Add about {{ing_02|1/2 cup (60 g)|0.5 cup}} flour; add more {{ing_02|flour|none}}.") == \
        "Add about 1/2 cup (60 g) flour; add more flour."


def _two_tin_refinement():
    return CayenneRefinement(
        title="Cake", base_servings=4,
        structured_ingredients=[],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Divide between {{pan_01|two tins}} and {{pan_02|a tray}}."),
        ],
        pans=[RecipePan(id="pan_01", raw="two tins", count=2), RecipePan(id="pan_02", raw="a tray")],
        bake_type="sponge",
        bake_step=0,
    )


def test_build_update_keeps_a_cooks_edited_tins():
    current = [{"id": "pan_01", "raw": "two tins", "count": 2, "shape": "round", "edited": True}]
    payload = build_update(_two_tin_refinement(), [0.1] * 3, read_rev=1, current_pans=current)
    assert payload["pans"] == current
    # pan_01 is the cook's and its words are in the step, so its token stays; pan_02 is gone, so it unwraps.
    assert payload["tokenized_directions"][0]["text"] == "Divide between {{pan_01|two tins}} and a tray."


def test_build_update_writes_refines_tins_when_none_is_edited():
    current = [{"id": "pan_01", "raw": "old", "edited": False}]
    ref = _two_tin_refinement()
    for cur in (current, None, []):
        payload = build_update(ref, [0.1] * 3, read_rev=1, current_pans=cur)
        assert payload["pans"] == [p.model_dump() for p in ref.pans]
        assert "{{pan_02|a tray}}" in payload["tokenized_directions"][0]["text"]


def _one_tin_refinement(words):
    return CayenneRefinement(
        title="Cake", base_servings=4, structured_ingredients=[],
        tokenized_directions=[TokenizedDirection(step=1, text="Bake in {{pan_01|%s}}." % words)],
        pans=[RecipePan(id="pan_01", raw=words)],
    )


def test_a_renumbered_tin_token_unwraps_but_the_cooks_own_words_stay():
    current = [{"id": "pan_01", "raw": "a  20CM round tin", "edited": True}]
    wrong = build_update(_one_tin_refinement("a 900g loaf tin"), [0.1], read_rev=1, current_pans=current)
    assert wrong["tokenized_directions"][0]["text"] == "Bake in a 900g loaf tin."
    right = build_update(_one_tin_refinement("20cm round tin"), [0.1], read_rev=1, current_pans=current)
    assert right["tokenized_directions"][0]["text"] == "Bake in {{pan_01|20cm round tin}}."


def test_edited_tins_with_no_pan_tokens_leave_the_directions_unchanged():
    ref = _one_tin_refinement("a tin")
    ref.tokenized_directions = [TokenizedDirection(step=1, text="Mix {{ing_01|flour}}. Bake.")]
    current = [{"id": "pan_01", "raw": "a tin", "edited": True}]
    payload = build_update(ref, [0.1], read_rev=1, current_pans=current)
    assert payload["pans"] == current
    assert payload["tokenized_directions"] == [{"step": 1, "text": "Mix {{ing_01|flour}}. Bake."}]


def test_a_non_dict_entry_in_current_pans_does_not_crash():
    current = ["junk", None, {"id": "pan_01", "raw": "two tins", "edited": True}]
    payload = build_update(_two_tin_refinement(), [0.1], read_rev=1, current_pans=current)
    assert payload["pans"] == current
    assert "{{pan_01|two tins}}" in payload["tokenized_directions"][0]["text"]
    assert "{{pan_02" not in payload["tokenized_directions"][0]["text"]
    assert build_update(_two_tin_refinement(), [0.1], read_rev=1, current_pans=["junk"])["pans"][0]["id"] == "pan_01"
