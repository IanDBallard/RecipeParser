"""The Fat Token grammar and the checks on what REFINE says (Cayenne's direction amounts design)."""
import logging

import pytest

from recipeparser.core.fat_tokens import (
    Use,
    check_mentions,
    is_quantity,
    is_tagged,
    parse_use,
    splice_mentions,
    strip_fat_tokens,
)
from recipeparser.models import DirectionMention, StructuredIngredient, TokenizedDirection

FLOUR = StructuredIngredient(id="ing_02", amount=0.75, unit="cup", name="all-purpose flour",
                             fallback_string="3/4 cup (90 g) all-purpose flour",
                             converted_amount=90, converted_unit="g")
RICOTTA = StructuredIngredient(id="ing_01", amount=425, unit="g", name="ricotta cheese, preferably whole milk",
                               fallback_string="425 g ricotta cheese, preferably whole milk")
SALT = StructuredIngredient(id="ing_03", name="Kosher salt", fallback_string="Kosher salt")
GNOCCHI = [RICOTTA, FLOUR, SALT]
GNOCCHI_STEPS = [
    "Drain the ricotta overnight.",
    "Add about 1/2 cup (60 g) flour and stir; add more flour until the mixture forms a very sticky dough.",
    "Salt the water.",
]


def _d(*texts):
    return [TokenizedDirection(step=i + 1, text=t) for i, t in enumerate(texts)]


@pytest.mark.parametrize("raw, expected", [
    (None, Use("legacy")),
    ("all", Use("all")),
    (" REST ", Use("rest")),
    ("none", Use("none")),
    ("0.5 cup", Use("part", 0.5, "cup")),
    ("60 g", Use("part", 60.0, "g")),
    ("2", Use("part", 2.0, None)),
    ("50ml", Use("part", 50.0, "ml")),
    ("0.5cup", Use("part", 0.5, "cup")),
    ("1/2 cup", Use("invalid")),
    ("0 cup", Use("invalid")),
    ("some", Use("invalid")),
])
def test_parse_use(raw, expected):
    assert parse_use(raw) == expected


def test_strip_gives_the_words_back_for_both_forms():
    text = "Mix {{ing_01|flour}} and {{ing_02|1/2 cup|0.5 cup}} sugar."
    assert strip_fat_tokens(text) == "Mix flour and 1/2 cup sugar."


def test_is_tagged():
    assert is_tagged(_d("Mix {{ing_01|flour|all}}.", "Bake."))
    assert not is_tagged(_d("Mix {{ing_01|flour|all}} and {{ing_02|eggs}}."))
    assert not is_tagged(_d("Bake."))


class TestCheckMentions:
    def test_a_consistent_recipe_is_unchanged(self):
        directions = _d(
            "Drain the {{ing_01|ricotta|all}} overnight.",
            "Add about {{ing_02|1/2 cup (60 g)|0.5 cup}} flour; add more {{ing_02|flour|none}}.",
        )
        assert check_mentions(GNOCCHI, directions) == (directions, [])

    def test_demotes_an_unparseable_amount(self):
        checked, demotions = check_mentions(GNOCCHI, _d("Add {{ing_02|half a cup|half a cup}} flour."))
        assert checked[0].text == "Add {{ing_02|half a cup|none}} flour."
        assert len(demotions) == 1

    def test_demotes_every_part_and_the_rest_when_parts_exceed_the_line(self):
        checked, _ = check_mentions(GNOCCHI, _d(
            "Add {{ing_02|1/2 cup|0.5 cup}} flour, then {{ing_02|1/2 cup|0.5 cup}} more.",
            "Dust with the remaining {{ing_02|flour|rest}}.",
        ))
        assert [d.text for d in checked] == [
            "Add {{ing_02|1/2 cup|none}} flour, then {{ing_02|1/2 cup|none}} more.",
            "Dust with the remaining {{ing_02|flour|none}}.",
        ]

    def test_allows_about_five_percent_over_and_skips_units_it_cannot_compare(self):
        within = _d("Add {{ing_02|3/4 cup|0.78 cups}} flour.")
        assert check_mentions(GNOCCHI, within)[1] == []
        other_unit = _d("Add {{ing_02|200 g|200 g}} flour.")
        assert check_mentions(GNOCCHI, other_unit)[1] == []

    def test_demotes_all_beside_a_part(self):
        directions = _d("Add {{ing_02|1/2 cup|0.5 cup}} flour.", "Knead in {{ing_02|the flour|all}}.")
        checked, _ = check_mentions(GNOCCHI, directions)
        assert checked[1].text == "Knead in {{ing_02|the flour|none}}."

    def test_leaves_legacy_tokens_alone(self):
        directions = _d("Mix {{ing_02|flour}} and more {{ing_02|flour}}.")
        assert check_mentions(GNOCCHI, directions) == (directions, [])


class TestSpliceMentions:
    def test_tags_the_gnocchi_without_changing_a_word(self):
        mentions = [
            DirectionMention(step=1, quote="ricotta", ingredient_id="ing_01", use="all"),
            DirectionMention(step=2, quote="1/2 cup (60 g)", ingredient_id="ing_02", use="0.5 cup"),
            DirectionMention(step=2, quote="flour", context="add more flour until", ingredient_id="ing_02", use="none"),
            DirectionMention(step=3, quote="Salt", ingredient_id="ing_03", use="None"),
        ]
        directions, dropped = splice_mentions(GNOCCHI_STEPS, mentions, ["ing_01", "ing_02", "ing_03"])
        assert dropped == []
        assert [d.text for d in directions] == [
            "Drain the {{ing_01|ricotta|all}} overnight.",
            "Add about {{ing_02|1/2 cup (60 g)|0.5 cup}} flour and stir; add more {{ing_02|flour|none}} until the "
            "mixture forms a very sticky dough.",
            "{{ing_03|Salt|none}} the water.",
        ]
        assert [strip_fat_tokens(d.text) for d in directions] == GNOCCHI_STEPS
        assert [d.step for d in directions] == [1, 2, 3]

    def test_places_a_repeated_word_after_the_previous_token(self):
        mentions = [
            DirectionMention(step=1, quote="flour", context="Sift flour,", ingredient_id="ing_02", use="0.5 cup"),
            DirectionMention(step=1, quote="flour", context="rest of the flour.", ingredient_id="ing_02", use="rest"),
        ]
        directions, _ = splice_mentions(["Sift flour, then add the rest of the flour."], mentions, ["ing_02"])
        assert directions[0].text == "Sift {{ing_02|flour|0.5 cup}}, then add the rest of the {{ing_02|flour|rest}}."

    def test_without_context_places_only_an_unambiguous_quote(self):
        steps = ["Add 1/2 cup flour; add more flour until sticky."]
        mentions = [
            DirectionMention(step=1, quote="1/2 cup", ingredient_id="ing_02", use="0.5 cup"),
            DirectionMention(step=1, quote="flour", ingredient_id="ing_02", use="none"),
        ]
        directions, dropped = splice_mentions(steps, mentions, ["ing_02"])
        assert directions[0].text == "Add {{ing_02|1/2 cup|0.5 cup}} flour; add more flour until sticky."
        assert len(dropped) == 1

    def test_a_context_that_is_not_in_the_step_falls_back_to_the_quote(self):
        mentions = [DirectionMention(step=1, quote="flour", context="stir in flour", ingredient_id="ing_02", use="all")]
        directions, _ = splice_mentions(["Add the flour."], mentions, ["ing_02"])
        assert directions[0].text == "Add the {{ing_02|flour|all}}."

    def test_drops_what_it_cannot_place_and_keeps_the_text(self):
        mentions = [
            DirectionMention(step=1, quote="plain flour", ingredient_id="ing_02", use="all"),
            DirectionMention(step=1, quote="flour", ingredient_id="ghost", use="all"),
            DirectionMention(step=1, quote="fl|our", ingredient_id="ing_02", use="all"),
            DirectionMention(step=9, quote="flour", ingredient_id="ing_02", use="all"),
        ]
        directions, dropped = splice_mentions(["Add the flour."], mentions, ["ing_02"])
        assert directions[0].text == "Add the flour."
        assert len(dropped) == 4


def test_refine_demotes_and_logs(caplog):
    from recipeparser.core.stages.refine import _check_mentions
    from recipeparser.models import CayenneRefinement
    refinement = CayenneRefinement(
        title="Gnocchi", base_servings=4, structured_ingredients=GNOCCHI,
        tokenized_directions=_d("Add {{ing_02|1/2 cup|0.5 cup}} flour.", "Knead in {{ing_02|the flour|all}}."),
    )
    with caplog.at_level(logging.WARNING):
        _check_mentions(refinement)
    assert refinement.tokenized_directions[1].text == "Knead in {{ing_02|the flour|none}}."
    assert "Gnocchi" in caplog.text


# The first live sample (2026-10-02, 20 recipes) showed the model putting amounts on names, the whole
# amount at every mention, and mentions out of order. Each case below is from that sample.
class TestTheFirstSample:
    BUTTER = StructuredIngredient(id="ing_01", amount=175, unit="g", name="butter", fallback_string="175 g butter")
    SUGAR = StructuredIngredient(id="ing_02", amount=50, unit="g", name="confectioners' sugar",
                                 fallback_string="50 g confectioners' sugar")
    PEEL = StructuredIngredient(id="ing_05", amount=1, name="lemon peel", fallback_string="grated peel of 1 lemon")
    OIL = StructuredIngredient(id="ing_09", amount=2, unit="tablespoons", name="canola oil",
                               fallback_string="2 tablespoons canola oil")
    PEAS = StructuredIngredient(id="ing_10", amount=1, unit="cup", name="peas", fallback_string="1 cup peas")
    BUTTERMILK = StructuredIngredient(id="ing_11", amount=400, unit="ml", name="buttermilk",
                                      fallback_string="400 ml buttermilk")
    RICE = StructuredIngredient(id="ing_12", amount=1.5, unit="cups", name="rice", fallback_string="1 1/2 cups rice")
    ALL = [BUTTER, SUGAR, PEEL, OIL, PEAS, BUTTERMILK, RICE]

    def _check(self, *texts):
        return [d.text for d in check_mentions(self.ALL, _d(*texts))[0]]

    def test_an_amount_on_the_name_that_is_the_whole_line_becomes_all(self):
        assert self._check(
            "Place the {{ing_01|butter|175 g}} and {{ing_02|confectioners’ sugar|50 g}} in the bowl; "
            "add grated {{ing_05|lemon peel|1}}."
        ) == [
            "Place the {{ing_01|butter|all}} and {{ing_02|confectioners’ sugar|all}} in the bowl; "
            "add grated {{ing_05|lemon peel|all}}."
        ]

    def test_an_amount_on_the_name_after_a_written_quantity_becomes_none(self):
        assert self._check(
            "Heat 1 tablespoon of the {{ing_09|canola oil|1 tablespoon}}.",
            "Add 1 cup {{ing_10|freshly shelled (or frozen) peas|1 cup}} to it.",
        ) == [
            "Heat 1 tablespoon of the {{ing_09|canola oil|none}}.",
            "Add 1 cup {{ing_10|freshly shelled (or frozen) peas|none}} to it.",
        ]

    def test_an_amount_on_the_name_that_is_a_share_becomes_none(self):
        assert self._check("Pour in half the {{ing_11|buttermilk|200 ml}}.") == [
            "Pour in half the {{ing_11|buttermilk|none}}."
        ]

    def test_a_quantity_in_words_keeps_its_amount(self):
        assert self._check("Immerse in water with {{ing_09|two tablespoons|2 tablespoons}} of the oil.") == [
            "Immerse in water with {{ing_09|two tablespoons|2 tablespoons}} of the oil."
        ]

    def test_only_the_first_all_keeps_it(self):
        assert self._check(
            "Wash the {{ing_12|rice|all}} well.",
            "Add the {{ing_12|rice|all}}, then drain the {{ing_12|rice|all}}.",
        ) == [
            "Wash the {{ing_12|rice|all}} well.",
            "Add the {{ing_12|rice|none}}, then drain the {{ing_12|rice|none}}.",
        ]

    def test_the_soda_bread_mentions_out_of_order_and_twice_are_all_placed(self):
        step = ("Put the flours, salt into a bowl. Draw the flour into the buttermilk. "
                "You may not need all the buttermilk, it depends on the flour you use.")
        mentions = [
            DirectionMention(step=1, quote="flours", context="Put the flours, salt", ingredient_id="ing_01",
                             use="none"),
            DirectionMention(step=1, quote="flour", context="depends on the flour you use", ingredient_id="ing_02",
                             use="none"),
            DirectionMention(step=1, quote="flour", context="Draw the flour into", ingredient_id="ing_01", use="none"),
            DirectionMention(step=1, quote="buttermilk", context="not need all the buttermilk,",
                             ingredient_id="ing_05", use="none"),
            DirectionMention(step=1, quote="flours", context="Put the flours, salt", ingredient_id="ing_01",
                             use="none"),
        ]
        directions, dropped = splice_mentions([step], mentions, ["ing_01", "ing_02", "ing_05"])
        assert dropped == []
        assert directions[0].text == (
            "Put the {{ing_01|flours|none}}, salt into a bowl. Draw the {{ing_01|flour|none}} into the buttermilk. "
            "You may not need all the {{ing_05|buttermilk|none}}, it depends on the {{ing_02|flour|none}} you use."
        )

    def test_a_curly_apostrophe_in_the_step_matches_a_straight_one_in_the_context(self):
        step = "Stir the pepper; they’ll smoke. Grind the pepper."
        mentions = [DirectionMention(step=1, quote="pepper", context="Grind the pepper.", ingredient_id="ing_01",
                                     use="none"),
                    DirectionMention(step=1, quote="pepper", context="the pepper; they'll smoke",
                                     ingredient_id="ing_01", use="all")]
        directions, dropped = splice_mentions([step], mentions, ["ing_01"])
        assert dropped == []
        assert directions[0].text == "Stir the {{ing_01|pepper|all}}; they’ll smoke. Grind the {{ing_01|pepper|none}}."

    def test_keeps_the_recipes_own_step_numbers(self):
        directions, _ = splice_mentions(["Mix.", "Bake."], [], [], [3, 4])
        assert [d.step for d in directions] == [3, 4]


# The second live sample (after 9.5.1): the model wrapped a quantity and the name together.
class TestTheSecondSample:
    PEAS = StructuredIngredient(id="ing_01", amount=1, unit="cup", name="peas",
                                fallback_string="1 cup freshly shelled (or frozen) peas")

    def test_an_amount_on_a_quantity_and_the_name_together_becomes_none(self):
        directions = _d("Add {{ing_01|1 cup freshly shelled (or frozen) peas|1 cup}} to it.")
        checked, changes = check_mentions([self.PEAS], directions)
        assert checked[0].text == "Add {{ing_01|1 cup freshly shelled (or frozen) peas|none}} to it."
        assert len(changes) == 1

    @pytest.mark.parametrize("words", [
        "1/2 cup (60 g)", "two tablespoons", "120ml", "40 grams", "1 packet", "about ½ cup",
        "2 heaped tablespoons", "1 1/2 cups", "3", "half a cup", "2 to 3 tbsp",
    ])
    def test_is_a_quantity(self, words):
        assert is_quantity(words)

    @pytest.mark.parametrize("words", [
        "1 cup freshly shelled (or frozen) peas", "2 large eggs", "butter", "175 g butter", "lemon peel",
    ])
    def test_is_not_a_quantity(self, words):
        assert not is_quantity(words)


# The third live sample (after 9.6.1): a whole-amount token right after a quantity the text writes.
class TestTheThirdSample:
    BANANAS = StructuredIngredient(id="ing_04", amount=4, name="bananas", fallback_string="4 ripe bananas")
    WHITES = StructuredIngredient(id="ing_09", amount=2, name="eggs", fallback_string="2 eggs")
    FLOUR = StructuredIngredient(id="ing_01", amount=2, unit="cups", name="flour", fallback_string="2 cups flour")

    def _check(self, *texts):
        return [d.text for d in check_mentions([self.BANANAS, self.WHITES, self.FLOUR], _d(*texts))[0]]

    def test_a_whole_amount_right_after_a_written_quantity_becomes_none(self):
        assert self._check(
            "Peel approximately 3 {{ing_04|bananas|all}} (325 grams peeled).",
            "Pour in the {{ing_04|bananas|all}}.",
        ) == [
            "Peel approximately 3 {{ing_04|bananas|none}} (325 grams peeled).",
            "Pour in the {{ing_04|bananas|none}}.",
        ]

    def test_a_remainder_right_after_a_number_word_becomes_none(self):
        assert self._check("Add one of the {{ing_09|whites|rest}}.") == ["Add one of the {{ing_09|whites|none}}."]

    @pytest.mark.parametrize("before", [
        "Preheat the oven to 350. Add the ", "Bake for 20 minutes, then add the ", "Cook 2 minutes then add the ",
    ])
    def test_a_number_in_an_earlier_clause_does_not_count(self, before):
        assert self._check(before + "{{ing_01|flour|all}}.") == [before + "{{ing_01|flour|all}}."]
