"""Estimating servings from the tins a recipe names (spec table)."""

import json
from pathlib import Path

from recipeparser.core.pans import HINT_KEYS
from recipeparser.core.tin_servings import HINT_TINS, estimate_from_tins
from recipeparser.models import PanDims, RecipePan

FIXTURE = Path(__file__).parent.parent / "fixtures" / "hint_tins.json"


def _pan(count=1, d=None, w=None, l=None, depth=None, **kw):  # noqa: E741
    return RecipePan(id="pan_01", raw="a tin", count=count, dims_mm=PanDims(d=d, w=w, l=l, depth=depth), **kw)


def test_hint_tins_match_fixture():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert {r["key"] for r in rows} == set(HINT_KEYS) == set(HINT_TINS)
    assert len(rows) == len(HINT_KEYS)
    for r in rows:
        assert HINT_TINS[r["key"]] == {k: v for k, v in r.items() if k != "key"}


def test_layer_cake_two_20cm():
    assert estimate_from_tins([_pan(2, d=200, family="round-cake")], "butter-cake") == 8


def test_two_23cm():
    assert estimate_from_tins([_pan(2, d=230, family="round-cake")], "butter-cake") == 11


def test_sheet_13x9_single():
    assert estimate_from_tins([_pan(l=330, w=230, family="traybake")], "butter-cake") == 14


def test_bars_9x13():
    assert estimate_from_tins([_pan(l=330, w=230, family="traybake")], "bar") == 24


def test_bars_9x9():
    assert estimate_from_tins([_pan(l=229, w=229, family="square")], "bar") == 17


def test_cheesecake_23_springform():
    assert estimate_from_tins([_pan(d=230, family="round-cake")], "cheesecake") == 12


def test_tart_23():
    assert estimate_from_tins([_pan(d=230, family="tart")], "pie-fruit") == 8


def test_loaf_8_5_inch():
    assert estimate_from_tins([_pan(l=216, w=114, family="loaf")], "quick-bread") == 10


def test_bundt_12_cup():
    assert estimate_from_tins([_pan(capacity_ml=2840, family="ring")], "butter-cake") == 12


def test_bundt_via_hint_without_dims():
    assert estimate_from_tins([_pan(catalog_hint="us-bundt-12")], "butter-cake") == 12


def test_hint_supplies_area_and_family():
    assert estimate_from_tins([_pan(catalog_hint="us-rect-13x9")], "bar") == 24


def test_deep_single_tin_is_a_layer_cake():
    assert estimate_from_tins([_pan(d=200, depth=75, family="round-cake")], "butter-cake") == 8


def test_shallow_single_tin_uses_the_single_layer_footprint():
    assert estimate_from_tins([_pan(d=200, depth=40, family="round-cake")], "butter-cake") == 6


def test_pizza_is_none():
    assert estimate_from_tins([_pan(d=300, family="pizza")], "pizza") is None
    assert estimate_from_tins([_pan(d=300, family="round-cake")], "pizza") is None


def test_stovetop_is_none():
    assert estimate_from_tins([_pan(d=200, family="stovetop")], "custard") is None


def test_null_family_is_none():
    assert estimate_from_tins([_pan(d=200)], "butter-cake") is None


def test_two_families_is_none():
    pans = [_pan(d=200, family="round-cake"), _pan(l=200, w=100, family="loaf")]
    assert estimate_from_tins(pans, "butter-cake") is None


def test_tombstone_is_skipped():
    gone = _pan(d=400, family="loaf", removed=True)
    assert estimate_from_tins([gone, _pan(2, d=200, family="round-cake")], "butter-cake") == 8
    assert estimate_from_tins([gone], "butter-cake") is None


def test_no_geometry_is_none():
    assert estimate_from_tins([_pan(family="round-cake")], "butter-cake") is None
    assert estimate_from_tins([], "butter-cake") is None


def test_at_least_one():
    assert estimate_from_tins([_pan(d=50, family="tart")], "pie-fruit") == 1


# --- whole-branch review: layer rule and bad tins (M1, M2) ---------------------


def test_two_13x9_traybakes_take_the_single_layer_footprint():
    # A traybake or sheet is never a layer cake: two 13x9 tins are 2 x 759 cm2 / 55 = 28.
    assert estimate_from_tins([_pan(2, l=330, w=230, family="traybake")], "butter-cake") == 28


def test_a_deep_sheet_is_not_a_layer_cake():
    assert estimate_from_tins([_pan(l=330, w=230, depth=80, family="sheet")], "butter-cake") == 14


def test_two_square_tins_are_a_layer_cake():
    assert estimate_from_tins([_pan(2, l=200, w=200, family="square")], "butter-cake") == 11


def test_a_tin_counted_below_one_is_skipped():
    assert estimate_from_tins([_pan(0, d=200, family="round-cake")], "butter-cake") is None
    assert estimate_from_tins([_pan(-2, d=200, family="round-cake")], "butter-cake") is None
    good = _pan(1, d=200, depth=40, family="round-cake")
    assert estimate_from_tins([_pan(0, d=900, family="round-cake"), good], "butter-cake") == 6


def test_non_finite_or_non_positive_dims_are_skipped():
    nan, inf = float("nan"), float("inf")
    for bad in (_pan(d=nan, family="round-cake"), _pan(d=inf, family="round-cake"),
                _pan(d=-200, family="round-cake"), _pan(l=330, w=0, family="traybake"),
                _pan(l=inf, w=230, family="traybake")):
        assert estimate_from_tins([bad], "butter-cake") is None, bad
    assert estimate_from_tins([_pan(l=nan, w=100, family="loaf")], None) is None


def test_non_finite_or_non_positive_capacity_is_skipped():
    for cap in (float("nan"), float("inf"), 0.0, -1000.0):
        assert estimate_from_tins([_pan(capacity_ml=cap, family="ring")], None) is None, cap
