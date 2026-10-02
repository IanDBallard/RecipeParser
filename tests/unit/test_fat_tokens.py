"""The Fat Token grammar and the checks on what REFINE says (Cayenne's direction amounts design)."""
import logging

import pytest

from recipeparser.core.fat_tokens import Use, check_mentions, is_tagged, parse_use, splice_mentions, strip_fat_tokens
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
