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


def _ing(id_, amount, unit, name):
    return StructuredIngredient(id=id_, kind="ingredient", amount=amount, unit=unit, name=name, fallback_string=name)


class TestAMeasuredPartBeforeAMention:
    """Cayenne Fix Roadmap F-499 and F-689: a measure the method writes scales with the list."""

    OIL = _ing("ing_01", 3, "tbsp", "olive oil")
    CREAM = _ing("ing_02", 160, "g", "coconut cream")
    WATER = _ing("ing_03", 800, "ml", "water")
    SUGAR = _ing("ing_04", 2, "tablespoons", "sugar")

    def _tag(self, texts, *ingredients):
        out, lines = tag_counts(_directions(texts), list(ingredients), set())
        return [d.text for d in out], lines

    def test_a_measure_right_before_a_none_mention_is_wrapped(self):
        out, lines = self._tag(["Heat 2 tbsp {{ing_01|olive oil|none}} in a pan."], self.OIL)
        assert out == ["Heat {{ing_01|2 tbsp|2 tbsp}} {{ing_01|olive oil|none}} in a pan."]
        assert lines

    def test_a_mention_holding_its_quantity_and_name_is_split(self):
        out, _ = self._tag(["Stir in 60g {{ing_02|coconut cream|none}} and {{ing_03|800ml water|none}}."],
                           self.CREAM, self.WATER)
        assert out == ["Stir in {{ing_02|60g|60 g}} {{ing_02|coconut cream|none}} and "
                       "{{ing_03|800ml|800 ml}} {{ing_03|water|none}}."]

    def test_the_unit_may_be_spelt_another_way(self):
        out, _ = self._tag(["Sprinkle 1 tablespoon {{ing_04|sugar|none}} over the top."], self.SUGAR)
        assert out == ["Sprinkle {{ing_04|1 tablespoon|1 tablespoons}} {{ing_04|sugar|none}} over the top."]

    def test_fractions_and_of_the(self):
        out, _ = self._tag(["Add 1½ tbsp of the {{ing_01|oil|none}}."], self.OIL)
        assert out == ["Add {{ing_01|1½ tbsp|1.5 tbsp}} of the {{ing_01|oil|none}}."]

    def test_parts_that_add_up_to_more_than_the_line_are_left_as_written(self):
        texts = ["Heat 2 tbsp {{ing_01|oil|none}}.", "Add 2 tbsp {{ing_01|oil|none}}."]
        out, lines = self._tag(texts, self.OIL)
        assert out == texts
        assert any("more than the line" in line for line in lines)

    def test_a_part_the_model_wrote_counts_toward_the_line(self):
        texts = ["Heat {{ing_01|2 tbsp|2 tbsp}} oil.", "Add 2 tbsp {{ing_01|oil|none}}."]
        out, _ = self._tag(texts, self.OIL)
        assert out == texts

    def test_a_rate_a_range_another_unit_or_a_count_is_left_alone(self):
        eggs = _ing("ing_05", 3, None, "eggs")
        texts = [
            "Sprinkle 1 tablespoon {{ing_04|sugar|none}} per serving.",
            "Use 1 to 2 tbsp {{ing_01|oil|none}}.",
            "Add 50 ml {{ing_02|coconut cream|none}}.",
            "Beat 2 {{ing_05|eggs|none}} with a fork.",
        ]
        out, _ = self._tag(texts, self.SUGAR, self.OIL, self.CREAM, eggs)
        assert out[:3] == texts[:3]

    def test_only_a_none_mention_takes_a_measure(self):
        texts = ["Heat 2 tbsp {{ing_01|olive oil|all}}."]
        out, _ = self._tag(texts, self.OIL)
        assert out[0].startswith("Heat 2 tbsp")

    def test_the_words_are_kept_and_a_second_pass_changes_nothing(self):
        texts = ["Heat 2 tbsp {{ing_01|olive oil|none}}.", "Stir in {{ing_03|800ml water|none}}."]
        once, _ = self._tag(texts, self.OIL, self.WATER)
        twice, _ = self._tag(once, self.OIL, self.WATER)
        assert twice == once
        assert [strip_fat_tokens(t) for t in once] == [strip_fat_tokens(t) for t in texts]

    def test_the_import_checks_keep_the_new_parts(self):
        out, _ = self._tag(["Heat 2 tbsp {{ing_01|olive oil|none}}."], self.OIL)
        checked, demotions = check_mentions([self.OIL], _directions(out))
        assert [d.text for d in checked] == out and demotions == []


class TestAYieldInTwoCuts:
    """F-499: "Cut into 4 strips, then cut each strip into six bars" makes 24; the 4 scales."""

    def test_the_first_cut_is_wrapped_when_the_product_is_the_yield(self):
        out, _ = tag_counts(_directions(["Cut into 4 strips, then cut each strip into six bars."]), [], {24})
        assert out[0].text == "Cut into {{yield:4|4}} strips, then cut each strip into six bars."

    def test_not_when_the_product_is_not_the_yield(self):
        texts = ["Cut into 4 strips, then cut each strip into six bars."]
        out, _ = tag_counts(_directions(texts), [], {16})
        assert out[0].text == texts[0]
