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


def test_component_lines_are_positions_mapped_to_ids_and_headings_are_dropped():
    a = YieldAnswer(kind="servings", container=YieldContainer(pastry="shortcrust", base=True, sides="full", top="full", thickness="standard", component_lines=[1, 2, 9], topping_lines=[3]))
    spec, warnings = check_yield(a, ING, [])
    assert spec["container"]["component_lines"] == ["ing_02"]
    assert spec["container"]["topping_lines"] == ["ing_03"]
    assert any("position 1" in w for w in warnings) and any("position 9" in w for w in warnings)


def test_unknown_kind_is_none_and_unknown_values_take_defaults():
    assert check_yield(YieldAnswer(kind="crusts"), ING, [])[0] is None
    spec, _ = check_yield(YieldAnswer(kind="component", container=YieldContainer(pastry="choux", sides="tall", top="full", thickness="standard", base=True)), ING, [])
    assert spec["container"]["pastry"] == "shortcrust" and spec["container"]["sides"] == "full"
    assert spec["edited"] is False


def test_a_component_never_carries_pieces_or_a_weight():
    a = YieldAnswer(kind="component", area={"count": 4, "noun": "round", "diameter_mm": 300}, pieces={"count": 4, "noun": "round", "weight_g": 250})
    spec, _ = check_yield(a, ING, [])
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
    spec, warnings = check_yield(YieldAnswer(kind="component", area={"count": 2, "noun": "round", "diameter_mm": 700}), ING, [])
    assert spec["area"] is None and warnings
    spec, _ = check_yield(YieldAnswer(kind="component", area={"count": 0, "noun": "round", "diameter_mm": 300}), ING, [])
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
        spec, warnings = check_yield(YieldAnswer(kind="component", area=YieldArea(**area)), ING, [])
        assert spec["area"] is None and warnings
    spec, _ = check_yield(YieldAnswer(kind="component", area={"count": 2.0, "diameter_mm": 300}), ING, [])
    assert spec["area"]["count"] == 2
    for pieces in ({"count": None}, {"count": 1.5}):
        spec, warnings = check_yield(YieldAnswer(kind="pieces", pieces=YieldPieces(**pieces)), ING, [])
        assert spec["pieces"] is None and warnings
    assert check_yield(YieldAnswer(), ING, [])[0] is None


def test_position_zero_and_negative_are_dropped():
    a = YieldAnswer(kind="component", container=YieldContainer(component_lines=[0, -1, 2.0, "x"]))
    spec, warnings = check_yield(a, ING, [])
    assert spec["container"]["component_lines"] == ["ing_02"]
    assert any("position 0" in w for w in warnings) and any("position -1" in w for w in warnings)


def test_a_line_in_both_lists_is_component_and_defaults_fill_missing_fields():
    a = YieldAnswer(kind="component", container=YieldContainer(pastry="puff", component_lines=[2], topping_lines=[2, 3]))
    spec, _ = check_yield(a, ING, [])
    c = spec["container"]
    assert c["component_lines"] == ["ing_02"] and c["topping_lines"] == ["ing_03"]
    assert (c["base"], c["sides"], c["top"], c["thickness"], c["pastry"]) == (True, "full", "none", "standard", "puff")
