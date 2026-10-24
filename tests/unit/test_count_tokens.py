"""Counts in the method that scale: yield counts and counts of an ingredient (Cayenne scaling text audit, 2026-10-24).

The shared fixture ``tests/fixtures/scaling_text_cases.json`` is the contract: Cayenne's client renders
each case's ``after``, and keeps an identical copy of the file.
"""

import json
from pathlib import Path

import pytest

from recipeparser.core.count_tokens import tag_counts, yield_numbers
from recipeparser.core.fat_tokens import check_mentions, is_special_id, is_yield_id, strip_fat_tokens
from recipeparser.models import StructuredIngredient, TokenizedDirection

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "scaling_text_cases.json"
CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


def _ingredients(case):
    return [
        StructuredIngredient(
            id=i["id"], kind="ingredient", amount=i["amount"], unit=i["unit"], name=i["name"], fallback_string=i["name"]
        )
        for i in case["ingredients"]
    ]


def _directions(texts):
    return [TokenizedDirection(step=n + 1, text=t) for n, t in enumerate(texts)]


def _yields(case):
    y = case["yield"]
    return yield_numbers(y["base"], y["min"], y["max"], y["note"])


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_shared_cases(case):
    out, _ = tag_counts(_directions(case["before"]), _ingredients(case), _yields(case))
    assert [d.text for d in out] == case["after"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_words_are_kept_exactly(case):
    out, _ = tag_counts(_directions(case["before"]), _ingredients(case), _yields(case))
    assert [strip_fat_tokens(d.text) for d in out] == [strip_fat_tokens(t) for t in case["before"]]


def test_yield_numbers():
    # A size is never a yield: the 9 of "9-inch", the 2 of 2".
    assert yield_numbers(16, None, None, "Yields one 9-inch-square pan of brownies") == {16, 1}
    assert yield_numbers(24, None, None, 'two dozen 2" brownies') == {24}
    assert yield_numbers(16, 16, 16, "2 inch bars") == {16}
    assert yield_numbers(None, 10, 12, None) == {10, 12}
    assert yield_numbers(None, None, None, None) == set()


def test_a_yield_token_is_special():
    assert is_yield_id("yield:16")
    assert is_special_id("yield:16")
    assert strip_fat_tokens("Cut into {{yield:16|sixteen}} squares") == "Cut into sixteen squares"


def test_a_count_is_tagged_only_when_it_is_the_recipes_yield():
    out, lines = tag_counts(_directions(["Cut into 10 squares."]), [], {16})
    assert out[0].text == "Cut into 10 squares."
    assert lines == []


def test_a_range_of_yields_is_two_tokens():
    out, _ = tag_counts(_directions(["Cut into 10-12 even rolls."]), [], {10, 12})
    assert out[0].text == "Cut into {{yield:10|10}}-{{yield:12|12}} even rolls."


def test_each_and_per_are_rates_never_yields():
    texts = ["Cut each half into 6 slices.", "Divide each piece into 6 balls."]
    out, _ = tag_counts(_directions(texts), [], {6})
    assert [d.text for d in out] == texts


def test_cutting_an_ingredient_counted_by_the_piece_is_not_the_yield():
    # Found by the backfill dry run: "cut the tomato into 4 wedges" in a recipe that serves 4.
    tomatoes = StructuredIngredient(
        id="ing_01", kind="ingredient", amount=2, unit=None, name="ripe tomatoes", fallback_string="2 ripe tomatoes"
    )
    lemon = StructuredIngredient(
        id="ing_02", kind="ingredient", amount=1, unit="large", name="lemon", fallback_string="1 large lemon"
    )
    texts = ["Cut the tomato into 4 wedges.", "Cut the remaining 1/2 large lemon into 4 wedges."]
    out, _ = tag_counts(_directions(texts), [tomatoes, lemon], {4})
    assert [d.text for d in out] == texts
    # The dough is not counted by the piece: dividing it is the yield.
    assert (
        tag_counts(_directions(["Roll the dough into 4 balls."]), [tomatoes], {4})[0][0].text
        == "Roll the dough into {{yield:4|4}} balls."
    )


def test_a_count_inside_a_token_is_left_alone():
    text = "Spread in the {{pan_01|9-inch pan}} and cut into {{yield:16|16}} squares."
    assert tag_counts(_directions([text]), [], {16, 9})[0][0].text == text


def test_a_count_after_or_is_one_end_of_a_range_and_left_alone():
    eggs = StructuredIngredient(
        id="ing_01", kind="ingredient", amount=2, unit=None, name="eggs", fallback_string="2 eggs"
    )
    text = "Beat in one or two {{ing_01|eggs|none}}."
    assert tag_counts(_directions([text]), [eggs], set())[0][0].text == text


def test_a_count_of_an_ingredient_at_a_time_is_a_rate():
    eggs = StructuredIngredient(
        id="ing_01", kind="ingredient", amount=4, unit=None, name="eggs", fallback_string="4 eggs"
    )
    text = "Add two {{ing_01|eggs|none}} at a time."
    assert tag_counts(_directions([text]), [eggs], set())[0][0].text == text


def test_the_count_carries_the_number_and_an_all_elsewhere_gives_way():
    eggs = StructuredIngredient(
        id="ing_04", kind="ingredient", amount=4, unit="large", name="eggs", fallback_string="4 large eggs"
    )
    out, _ = tag_counts(
        _directions(["Beat the {{ing_04|eggs|all}}.", "Fold in all four of the {{ing_04|eggs|none}}."]), [eggs], set()
    )
    checked, _ = check_mentions([eggs], out)
    assert [d.text for d in checked] == [
        "Beat the {{ing_04|eggs|none}}.",
        "Fold in all {{ing_04|four|4}} of the {{ing_04|eggs|none}}.",
    ]


class TestNoAmountWhereTheSourceWroteNone:
    """F-495: "Measure all the ingredients" read "Measure all the 225 g ingredients"."""

    BUTTER = StructuredIngredient(
        id="ing_01",
        kind="ingredient",
        amount=225,
        unit="g",
        name="butter or baking spread",
        fallback_string="225g butter",
    )
    DOUGH = StructuredIngredient(
        id="ing_02", kind="ingredient", amount=1, unit="lb", name="raw pizza dough", fallback_string="1 lb pizza dough"
    )

    @pytest.mark.parametrize(
        "words", ["ingredients", "dry ingredients", "mixture", "everything", "all", "them", "batter"]
    )
    def test_generic_words_are_demoted(self, words):
        out, lines = check_mentions(
            [self.BUTTER], _directions(["Measure all the {{ing_01|" + words + "|all}} into a bowl."])
        )
        assert out[0].text == "Measure all the {{ing_01|" + words + "|none}} into a bowl."
        assert lines

    def test_an_ingredient_named_by_that_word_keeps_its_amount(self):
        text = "Stretch the {{ing_02|dough|all}} thin."
        assert check_mentions([self.DOUGH], _directions([text]))[0][0].text == text
