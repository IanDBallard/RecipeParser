"""core/yields.py - the yield-kind vocabularies and check_yield (Cayenne yield kinds 11.3)."""
import hashlib
import json
from pathlib import Path

from recipeparser.core.yields import PASTRY_TYPES, SIDES, THICKNESSES, TOPS, YIELD_KINDS, check_yield
from recipeparser.models import RecipePan, StructuredIngredient, YieldAnswer, YieldArea, YieldContainer, YieldPieces

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "yield_kinds.json"
# The fixture is one file kept in two repositories: this constant also lives in Cayenne
# cayenne-web/tests/unit/domain/yieldKinds.test.ts, and both change together with the fixture.
_FIXTURE_SHA256 = "d1636dc4c63f659de2d05d86c80e618ef061dad000a244d7f688d0e9db8d73b1"


def test_fixture_is_byte_identical_to_the_copy_cayenne_runs():
    assert hashlib.sha256(_FIXTURE.read_bytes()).hexdigest() == _FIXTURE_SHA256


def test_vocabularies_match_the_fixture():
    f = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert tuple(f["kinds"]) == YIELD_KINDS and tuple(f["pastry"]) == PASTRY_TYPES
    assert tuple(f["sides"]) == SIDES and tuple(f["tops"]) == TOPS and tuple(f["thicknesses"]) == THICKNESSES


def _ing(i, kind="ingredient"):
    return StructuredIngredient(id=f"ing_{i:02d}", kind=kind, amount=None, unit=None, name=f"line {i}", fallback_string=f"line {i}", converted_amount=None, converted_unit=None, is_ai_converted=False, state=None, line_index=i - 1)


ING = [_ing(1, "heading"), _ing(2), _ing(3), _ing(4)]
PIE = [RecipePan(id="pan_01", raw="a 9 inch pie plate", family="pie")]


def test_component_lines_are_positions_mapped_to_ids_and_headings_are_dropped():
    a = YieldAnswer(kind="servings", container=YieldContainer(pastry="shortcrust", base=True, sides="full", top="full", thickness="standard", component_lines=[1, 2, 9], topping_lines=[3]))
    spec, warnings = check_yield(a, ING, [])
    assert spec["container"]["component_lines"] == ["ing_02"]
    assert spec["container"]["topping_lines"] == ["ing_03"]
    assert any("position 1" in w for w in warnings) and any("position 9" in w for w in warnings)


def test_unknown_kind_is_none_and_unknown_values_take_defaults():
    assert check_yield(YieldAnswer(kind="crusts"), ING, [])[0] is None
    spec, _ = check_yield(YieldAnswer(kind="component", container=YieldContainer(pastry="choux", sides="tall", top="full", thickness="standard", base=True)), ING, PIE)
    assert spec["container"]["pastry"] == "shortcrust" and spec["container"]["sides"] == "full"
    assert spec["edited"] is False


def test_a_component_never_carries_pieces_or_a_weight():
    a = YieldAnswer(kind="component", area={"count": 4, "noun": "round", "diameter_mm": 300}, pieces={"count": 4, "noun": "round", "weight_g": 250})
    spec, _ = check_yield(a, ING, PIE)
    assert spec["area"] == {"count": 4, "noun": "round", "diameter_mm": 300} and spec["pieces"] is None


def test_pieces_keeps_a_count_and_refuses_a_size_outside_range():
    spec, _ = check_yield(YieldAnswer(kind="pieces", pieces={"count": 2, "noun": "loaf", "weight_g": 450000}), ING, [])
    assert spec["pieces"]["count"] == 2 and spec["pieces"]["weight_g"] is None


def test_none_answer_is_none():
    assert check_yield(None, ING, []) == (None, [])


def test_area_is_dropped_off_a_component_and_when_out_of_range():
    area = {"count": 2, "noun": "round", "diameter_mm": 300}
    spec, warnings = check_yield(YieldAnswer(kind="servings", area=area), ING, [])
    assert spec["area"] is None and warnings
    spec, warnings = check_yield(YieldAnswer(kind="component", area={"count": 2, "noun": "round", "diameter_mm": 700}), ING, PIE)
    assert spec["area"] is None and warnings
    spec, _ = check_yield(YieldAnswer(kind="component", area={"count": 0, "noun": "round", "diameter_mm": 300}), ING, PIE)
    assert spec["area"] is None


def test_pieces_is_dropped_without_a_count_or_off_a_pieces_kind():
    spec, _ = check_yield(YieldAnswer(kind="pieces", pieces={"count": 0, "noun": "roll"}), ING, [])
    assert spec["pieces"] is None
    spec, _ = check_yield(YieldAnswer(kind="servings", pieces={"count": 2, "noun": "roll"}), ING, [])
    assert spec["pieces"] is None


def test_a_servings_container_with_null_pastry_is_kept_with_the_default():
    dish = RecipePan(id="pan_01", raw="a casserole dish", family="square")
    a = YieldAnswer(kind="servings", container=YieldContainer(pastry=None, base=False, top="full"))
    spec, warnings = check_yield(a, ING, [dish])
    assert spec["container"]["pastry"] == "shortcrust" and spec["container"]["top"] == "full"
    assert any("pastry" in w for w in warnings)
    assert check_yield(a, ING, [])[0]["container"] is not None


def test_a_container_with_no_base_and_no_top_is_dropped():
    a = YieldAnswer(kind="servings", container=YieldContainer(pastry="puff", base=False, top="none"))
    spec, warnings = check_yield(a, ING, [])
    assert spec["container"] is None and warnings


def test_bad_numbers_validate_and_are_dropped_by_the_check():
    for area in ({"count": None, "diameter_mm": 300}, {"count": 2.5, "diameter_mm": 300}, {"count": 2, "diameter_mm": None}):
        spec, warnings = check_yield(YieldAnswer(kind="component", area=YieldArea(**area)), ING, PIE)
        assert spec["area"] is None and warnings
    spec, _ = check_yield(YieldAnswer(kind="component", area={"count": 2.0, "diameter_mm": 300}), ING, PIE)
    assert spec["area"]["count"] == 2
    for pieces in ({"count": None}, {"count": 1.5}):
        spec, warnings = check_yield(YieldAnswer(kind="pieces", pieces=YieldPieces(**pieces)), ING, PIE)
        assert spec["pieces"] is None and warnings
    assert check_yield(YieldAnswer(), ING, PIE)[0] is None


def test_position_zero_and_negative_are_dropped():
    a = YieldAnswer(kind="component", container=YieldContainer(component_lines=[0, -1, 2.0, "x"]))
    spec, warnings = check_yield(a, ING, PIE)
    assert spec["container"]["component_lines"] == ["ing_02"]
    assert any("position 0" in w for w in warnings) and any("position -1" in w for w in warnings)


def test_a_line_in_both_lists_is_component_and_defaults_fill_missing_fields():
    a = YieldAnswer(kind="component", container=YieldContainer(pastry="puff", component_lines=[2], topping_lines=[2, 3]))
    spec, _ = check_yield(a, ING, PIE)
    c = spec["container"]
    assert c["component_lines"] == ["ing_02"] and c["topping_lines"] == ["ing_03"]
    assert (c["base"], c["sides"], c["top"], c["thickness"], c["pastry"]) == (True, "full", "none", "standard", "puff")


def test_a_component_with_a_pie_plate_stays_a_component():
    spec, warnings = check_yield(YieldAnswer(kind="component", container=YieldContainer(pastry="shortcrust")), ING, PIE)
    assert spec["kind"] == "component" and not any("tin" in w for w in warnings)


def test_a_component_with_no_tin_is_stored_as_servings_and_keeps_its_container():
    a = YieldAnswer(kind="component", container=YieldContainer(pastry="puff", top="full", component_lines=[2]))
    spec, warnings = check_yield(a, ING, [])
    assert spec["kind"] == "servings"
    assert spec["container"]["pastry"] == "puff" and spec["container"]["component_lines"] == []
    assert any("no fittable tin" in w for w in warnings)


def test_a_pizza_dough_with_an_area_and_no_tin_is_servings_with_the_area_kept():
    a = YieldAnswer(kind="component", area={"count": 4, "noun": "round", "diameter_mm": 300})
    spec, warnings = check_yield(a, ING, [])
    assert spec["kind"] == "servings" and spec["area"] == {"count": 4, "noun": "round", "diameter_mm": 300}
    assert any("until stage 2" in w for w in warnings)


def test_a_component_whose_only_tin_is_a_sheet_a_loaf_or_removed_is_servings():
    for family, removed in (("sheet", False), ("loaf", False), ("pie", True)):
        tins = [RecipePan(id="pan_01", raw="a tin", family=family, removed=removed)]
        assert check_yield(YieldAnswer(kind="component"), ING, tins)[0]["kind"] == "servings"
    tins = [RecipePan(id="pan_01", raw="a tin", family="sheet"), RecipePan(id="pan_02", raw="a tin", family="tart")]
    assert check_yield(YieldAnswer(kind="component"), ING, tins)[0]["kind"] == "component"


def test_a_crumb_crust_in_a_pie_plate_is_servings_and_a_shortcrust_crust_stays_a_component():
    crumb, warnings = check_yield(YieldAnswer(kind="component", container=YieldContainer(pastry="crumb", base=True)), ING, PIE)
    assert crumb["kind"] == "servings" and crumb["container"]["pastry"] == "crumb"
    assert any("crumb crust stored as servings until stage 3 (D84)" in w for w in warnings)
    short, _ = check_yield(YieldAnswer(kind="component", container=YieldContainer(pastry="shortcrust", base=True)), ING, PIE)
    assert short["kind"] == "component"


# --- hardening (whole-branch review, Minor 1) ---


def test_whole_is_none_for_a_huge_or_non_finite_number():
    from recipeparser.core.yields import _whole

    assert _whole(10**400) is None
    assert _whole(float("inf")) is None and _whole(float("nan")) is None
    assert _whole(2**60) is None
    assert _whole(12) == 12 and _whole(3.0) == 3


def test_a_huge_position_is_dropped_not_raised():
    container = YieldContainer(pastry="shortcrust", base=True, component_lines=[10**400, 2])
    a = YieldAnswer(kind="servings", container=container)
    spec, warnings = check_yield(a, ING, [])
    assert spec["container"]["component_lines"] == ["ing_02"]
    assert any("out of range" in w for w in warnings)


def test_a_piece_diameter_outside_10_to_1000_mm_is_dropped():
    for bad in (float("inf"), json.loads("1e999"), 5, 1001, float("nan")):
        answer = YieldAnswer(kind="pieces", pieces={"count": 4, "noun": "roll", "diameter_mm": bad})
        spec, warnings = check_yield(answer, ING, [])
        assert spec["pieces"]["diameter_mm"] is None, bad
        assert any("diameter" in w for w in warnings), bad
    spec, _ = check_yield(YieldAnswer(kind="pieces", pieces={"count": 4, "noun": "pizza", "diameter_mm": 300}), ING, [])
    assert spec["pieces"]["diameter_mm"] == 300
    assert json.dumps(spec, allow_nan=False)


def test_a_demotion_to_servings_empties_both_line_lists():
    c = YieldContainer(pastry="shortcrust", component_lines=[2, 3], topping_lines=[4])
    tinless, _ = check_yield(YieldAnswer(kind="component", container=c), ING, [])
    assert tinless["kind"] == "servings" and tinless["container"]["pastry"] == "shortcrust"
    assert tinless["container"]["component_lines"] == [] and tinless["container"]["topping_lines"] == []
    crumb = YieldContainer(pastry="crumb", component_lines=[2, 3], topping_lines=[4])
    spec, _ = check_yield(YieldAnswer(kind="component", container=crumb), ING, PIE)
    assert spec["kind"] == "servings" and spec["container"]["component_lines"] == []
    assert spec["container"]["topping_lines"] == []
    kept, _ = check_yield(YieldAnswer(kind="component", container=c), ING, PIE)
    assert kept["container"]["component_lines"] == ["ing_02", "ing_03"]


def test_a_crumble_with_no_rolled_piece_moves_its_component_lines_to_the_topping():
    c = YieldContainer(pastry="crumb", base=False, sides="none", top="crumble", component_lines=[2, 3], topping_lines=[4, 3])
    spec, warnings = check_yield(YieldAnswer(kind="servings", container=c), ING, [])
    assert spec["container"]["component_lines"] == []
    assert spec["container"]["topping_lines"] == ["ing_04", "ing_02", "ing_03"]
    assert any("topping_lines" in w and "moved" in w for w in warnings)
    rolled = YieldContainer(base=True, top="crumble", component_lines=[2], topping_lines=[3])
    spec, _ = check_yield(YieldAnswer(kind="servings", container=rolled), ING, [])
    assert spec["container"]["component_lines"] == ["ing_02"] and spec["container"]["topping_lines"] == ["ing_03"]
