from recipeparser.core.fat_tokens import strip_fat_tokens
from recipeparser.core.pans import (
    HINT_KEYS,
    apply_pan_checks,
    check_bake,
    check_core_flags,
    check_pan_tokens,
    mark_core,
    normalise_pans,
    place_pan_mentions,
)
from recipeparser.models import CayenneRefinement, RecipePan, TokenizedDirection


def pan(**kw):
    base = {
        "id": "pan_01",
        "raw": "two 20cm tins",
        "count": 2,
        "shape": "round",
        "family": "round-cake",
        "dims_mm": {"d": 200},
        "capacity_ml": None,
        "catalog_hint": None,
    }
    base.update(kw)
    return RecipePan.model_validate(base)


def steps(*texts):
    return [TokenizedDirection(step=i + 1, text=t) for i, t in enumerate(texts)]


def test_normalise_keeps_a_sized_tin_and_drops_an_unsized_one():
    kept, warnings = normalise_pans([pan(), pan(id="pan_02", raw="a cake tin", dims_mm={}, shape=None)])
    assert [p.id for p in kept] == ["pan_01"] and len(warnings) == 1


def test_normalise_nulls_unknown_vocab_and_bad_numbers_and_forces_edited_false():
    kept, _ = normalise_pans(
        [
            pan(
                shape="blob",
                family="cake",
                count=0,
                dims_mm={"d": 5000, "depth": 40},
                catalog_hint="made-up",
                edited=True,
            )
        ]
    )
    # d out of range is dropped; depth alone cannot size it, so the tin goes
    assert kept == []
    kept, _ = normalise_pans([pan(shape="blob", family="cake", count=0, edited=True)])
    p = kept[0]
    assert p.shape is None and p.family is None and p.count == 1 and p.edited is False


def test_normalise_keeps_a_hint_only_tin_from_the_list_and_drops_duplicate_ids():
    kept, _ = normalise_pans(
        [pan(raw="a 2 lb loaf tin", shape="loaf", family="loaf", dims_mm={}, catalog_hint="uk-loaf-2lb"), pan()]
    )
    assert [p.catalog_hint for p in kept] == ["uk-loaf-2lb"]
    assert "uk-loaf-2lb" in HINT_KEYS


def test_check_pan_tokens_unwraps_unknown_ids_and_words_the_source_never_wrote():
    out, warnings = check_pan_tokens(
        steps("Grease {{pan_01|two 20cm tins}}.", "Pour into {{pan_09|the tins}}.", "Line {{pan_01|three tins}}."),
        {"pan_01"},
        "Grease two 20cm tins.\nPour into the tins.\nLine the tins.",
    )
    assert [d.text for d in out] == ["Grease {{pan_01|two 20cm tins}}.", "Pour into the tins.", "Line three tins."]
    assert len(warnings) == 2


def test_check_bake_keeps_known_type_and_in_range_step_not_a_heading():
    d = steps("Heat the oven.", "**Bake**", "Bake 25 min.")
    assert check_bake("sponge", 2, d, {1}) == ("sponge", 2)
    assert check_bake("souffle", 2, d, {1}) == (None, None)
    assert check_bake(None, 2, d, {1}) == (None, None)
    assert check_bake("sponge", 1, d, {1}) == ("sponge", None)
    assert check_bake("sponge", 7, d, set()) == ("sponge", None)


def test_core_flags_survive_only_on_a_probe_temperature():
    out, warnings = check_core_flags(
        steps("until it reads {{temp:93C,core|93°C}}", "to {{temp:200F,core|200°F}}", "oil at {{temp:180C,core|180°C}}")
    )
    assert [d.text for d in out] == [
        "until it reads {{temp:93C,core|93°C}}",
        "to {{temp:200F,core|200°F}}",
        "oil at {{temp:180C|180°C}}",
    ]
    assert len(warnings) == 1


def test_apply_pan_checks_wires_everything_and_unwraps_tokens_of_dropped_tins():
    r = CayenneRefinement(
        title="T",
        base_servings=8,
        structured_ingredients=[],
        tokenized_directions=steps("Grease {{pan_01|two 20cm tins}} and {{pan_02|a tin}}.", "Bake."),
        pans=[pan(), pan(id="pan_02", raw="a tin", dims_mm={}, shape=None)],
        bake_type="sponge",
        bake_step=1,
    )
    warnings = apply_pan_checks(r, ["Grease two 20cm tins and a tin.", "Bake."])
    assert [p.id for p in r.pans] == ["pan_01"]
    assert r.tokenized_directions[0].text == "Grease {{pan_01|two 20cm tins}} and a tin."
    assert (r.bake_type, r.bake_step) == ("sponge", 1) and warnings


def test_heading_steps_is_one_bold_span_with_an_optional_colon():
    from recipeparser.core.pans import heading_steps

    d = steps("**Bake**", "**For the filling**:", "**Bake** at 180C then **cool**", "Bake.", "**a** and **b**")
    assert heading_steps(d) == {0, 1}


def test_normalise_nulls_the_hint_of_a_sized_tin_and_keeps_it():
    kept, _ = normalise_pans([pan(catalog_hint="made-up")])
    assert [p.id for p in kept] == ["pan_01"] and kept[0].catalog_hint is None


def test_normalise_duplicate_id_keeps_only_the_first_tin():
    kept, warnings = normalise_pans(
        [pan(raw="a 2 lb loaf tin", shape="loaf", family="loaf", dims_mm={}, catalog_hint="uk-loaf-2lb"), pan()]
    )
    assert len(kept) == 1 and kept[0].raw == "a 2 lb loaf tin" and len(warnings) == 1


def test_normalise_rejects_an_id_with_a_trailing_newline_or_a_short_number():
    kept, warnings = normalise_pans([pan(id="pan_01\n"), pan(id="pan_1")])
    assert kept == [] and len(warnings) == 2


def test_core_band_edges_are_inclusive_and_fahrenheit_is_converted():
    out, warnings = check_core_flags(
        steps(
            "{{temp:86F,core|86°F}}", "{{temp:230F,core|230°F}}", "{{temp:29C,core|29°C}}", "{{temp:111C,core|111°C}}"
        )
    )
    assert [d.text for d in out] == [
        "{{temp:86F,core|86°F}}",
        "{{temp:230F,core|230°F}}",
        "{{temp:29C|29°C}}",
        "{{temp:111C|111°C}}",
    ]
    assert len(warnings) == 2


def test_core_on_an_oven_or_gas_reading_loses_the_flag_and_keeps_the_token():
    out, warnings = check_core_flags(
        steps("{{temp:180C,oven,core|180°C}}", "{{temp:gas4,core|gas 4}}", "{{temp:93C,core,oven|93°C}}")
    )
    assert [d.text for d in out] == ["{{temp:180C,oven|180°C}}", "{{temp:gas4|gas 4}}", "{{temp:93C,oven|93°C}}"]
    assert len(warnings) == 3


def test_malformed_core_value_is_left_untouched_without_a_warning():
    texts = ["{{temp:93c,core|93°C}}", "{{temp:garbage,core|x}}"]
    out, warnings = check_core_flags(steps(*texts))
    assert [d.text for d in out] == texts and warnings == []


def test_empty_directions_do_not_crash():
    assert check_core_flags([]) == ([], [])
    assert check_pan_tokens([], set(), "") == ([], [])
    assert check_bake("sponge", 0, [], set()) == ("sponge", None)


def test_negative_bake_step_is_nulled():
    assert check_bake("sponge", -1, steps("Bake."), set()) == ("sponge", None)


def test_a_bad_id_such_as_pan_1_is_unwrapped():
    out, warnings = check_pan_tokens(steps("Grease {{pan_1|two tins}}."), {"pan_01"}, "Grease two tins.")
    assert [d.text for d in out] == ["Grease two tins."] and len(warnings) == 1


def test_apply_pan_checks_drops_an_unknown_bake_type_and_step_with_a_warning():
    r = CayenneRefinement(
        title="T",
        base_servings=8,
        structured_ingredients=[],
        tokenized_directions=steps("Mix.", "Bake."),
        bake_type="souffle",
        bake_step=1,
    )
    warnings = apply_pan_checks(r, ["Mix.", "Bake."])
    assert (r.bake_type, r.bake_step) == (None, None)
    assert "bake type 'souffle' dropped" in warnings


# --- Task 6: placing the backfill's answer ---------------------------------------------------
def test_place_pan_mentions_wraps_a_free_quote_once():
    text, dropped = place_pan_mentions("Grease two 20cm tins. Bake.", [("two 20cm tins", "", "pan_01")])
    assert text == "Grease {{pan_01|two 20cm tins}}. Bake." and dropped == []


def test_place_pan_mentions_drops_a_missing_or_ambiguous_quote_and_never_enters_a_token():
    text, dropped = place_pan_mentions(
        "Use a tin. Use a tin. {{ing_01|tin foil|all}}",
        [("a tin", "", "pan_01"), ("tin foil", "", "pan_02"), ("x", "", "bad")],
    )
    assert text == "Use a tin. Use a tin. {{ing_01|tin foil|all}}" and len(dropped) == 3


def test_place_pan_mentions_never_changes_a_word_outside_the_token():
    src = "Line the “20cm” tin — then {{ing_01|butter|all}} and {{temp:180C,oven|180°C}}; fill the 20cm tin."
    out, dropped = place_pan_mentions(
        src, [("the “20cm” tin", "", "pan_01"), ("fill the 20cm tin", "", "pan_01"), ("nope", "", "pan_01")]
    )
    assert out != src and len(dropped) == 1
    assert strip_fat_tokens(out) == strip_fat_tokens(src)


def test_mark_core_adds_the_flag_to_the_matching_temperature_token_only():
    text, notes = mark_core("Bake at {{temp:180C,oven|180°C}} until {{temp:93C|93°C}} in the centre.", ["93°C"])
    assert text == "Bake at {{temp:180C,oven|180°C}} until {{temp:93C,core|93°C}} in the centre."
    text, notes = mark_core("Bake at {{temp:180C,oven|180°C}}.", ["180°C"])
    assert "core" not in text and notes  # an oven token never takes core


def test_mark_core_leaves_an_out_of_band_fan_gas_pan_or_ingredient_token_alone():
    for src, quote in [
        ("Bake at {{temp:250C|250°C}}.", "250°C"),
        ("Bake at {{temp:180C,fan=160C|180°C}}.", "180°C"),
        ("Bake at {{temp:gas4|gas 4}}.", "gas 4"),
        ("Grease {{pan_01|20cm tin}}.", "20cm tin"),
        ("Add {{ing_01|flour|all}}.", "flour"),
    ]:
        text, notes = mark_core(src, [quote])
        assert text == src and notes, src


def test_place_pan_mentions_needs_a_word_boundary_and_never_cuts_a_token():
    out, dropped = place_pan_mentions("Use tinning.", [("tin", "", "pan_01")])
    assert out == "Use tinning." and len(dropped) == 1
    out, dropped = place_pan_mentions("Use the tin.", [("tin", "", "pan_01")])
    assert out == "Use the {{pan_01|tin}}." and dropped == []
    src = "Use {{ing_01|foil|all}} tin."
    out, dropped = place_pan_mentions(src, [("foil tin", "", "pan_01")])  # spans a token boundary
    assert out == src and len(dropped) == 1


def test_place_pan_mentions_drops_overlapping_quotes():
    out, dropped = place_pan_mentions(
        "Grease two 20cm tins well.", [("two 20cm tins", "", "pan_01"), ("20cm tins", "", "pan_02")]
    )
    assert out == "Grease {{pan_01|two 20cm tins}} well." and len(dropped) == 1


def test_mark_core_reads_fahrenheit_and_leaves_an_already_core_token_silent():
    text, notes = mark_core("Cook to {{temp:200F|200°F}}.", ["200°F"])
    assert text == "Cook to {{temp:200F,core|200°F}}." and notes == []
    src = "Cook to {{temp:93C,core|93°C}}."
    assert mark_core(src, ["93°C"]) == (src, [])


def test_mark_core_marks_none_when_a_quote_fits_two_tokens():
    src = "Reach {{temp:93C|93°C}}, then {{temp:93C|93°C}} again."
    text, notes = mark_core(src, ["93°C"])
    assert text == src and len(notes) == 1 and "ambiguous" in notes[0]


def test_core_check_decides_by_trimmed_parts_not_the_substring():
    out, warnings = check_core_flags(steps("oil at {{temp:180C, core|180°C}}", "in {{temp:180C,oven, core|180°C}}"))
    assert [d.text for d in out] == ["oil at {{temp:180C|180°C}}", "in {{temp:180C,oven|180°C}}"]
    assert len(warnings) == 2
