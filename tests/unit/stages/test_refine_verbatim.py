"""REFINE: the writer's two measures, AI conversions in g or ml, and a verified source system (D3, D5)."""
from unittest.mock import MagicMock, patch

from recipeparser.core.stages.refine import refine
from recipeparser.models import CayenneRefinement, RecipeExtraction, StructuredIngredient, TokenizedDirection

_PATCH = "recipeparser.core.stages.refine.refine_recipe_for_cayenne"

RAW = RecipeExtraction(
    name="Spaghetti Carbonara",
    ingredients=[
        "1 ounce (about 1/3 packed cup) grated pecorino Romano, plus additional for serving",
        "1 tablespoon olive oil",
    ],
    directions=["Cook the spaghetti."],
)


def _pecorino(**over) -> StructuredIngredient:
    fields = dict(
        id="ing_01", amount=1.0, unit="ounce", name="grated pecorino Romano",
        fallback_string=RAW.ingredients[0], converted_amount=0.333, converted_unit="cup",
        is_ai_converted=False, line_index=0,
    )
    fields.update(over)
    return StructuredIngredient(**fields)


def _oil(**over) -> StructuredIngredient:
    fields = dict(
        id="ing_02", amount=1.0, unit="tablespoon", name="olive oil", fallback_string=RAW.ingredients[1],
        converted_amount=13.5, converted_unit="g", is_ai_converted=True, line_index=1,
    )
    fields.update(over)
    return StructuredIngredient(**fields)


def _refinement(*ingredients, detected=None, evidence=None) -> CayenneRefinement:
    return CayenneRefinement(
        title="Spaghetti Carbonara", base_servings=4, structured_ingredients=list(ingredients),
        tokenized_directions=[TokenizedDirection(step=1, text="Cook the spaghetti.")],
        source_uom_system_detected=detected, source_uom_system_evidence=evidence,
    )


def _refine(refinement, source_host=None, raw=RAW):
    with patch(_PATCH, return_value=refinement) as fn:
        return refine(raw, client=MagicMock(), source_host=source_host), fn


class TestTheWritersSecondMeasure:
    def test_a_writer_measure_in_its_own_line_stays_the_writers(self):
        result, _ = _refine(_refinement(_pecorino()))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (0.333, "cup", False)

    def test_a_measure_its_line_never_writes_is_re_marked_as_the_ais_and_an_ai_cup_dropped(self):
        result, _ = _refine(_refinement(_pecorino(converted_amount=0.25)))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (None, None, False)

    def test_a_re_marked_measure_of_the_other_kind_survives_as_the_ais(self):
        result, _ = _refine(_refinement(_pecorino(converted_amount=80.0, converted_unit="ml")))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (80.0, "ml", True)


class TestAiConversions:
    def test_an_ai_conversion_in_grams_or_millilitres_is_kept(self):
        honey = _oil(id="ing_03", amount=20.0, unit="g", name="honey", converted_amount=15.0, converted_unit="ml")
        result, _ = _refine(_refinement(_oil(), honey))
        assert [i.converted_unit for i in result.structured_ingredients] == ["g", "ml"]

    # Final review of imperial measures: converted_* is the OTHER measure (verbatim ingestion D3). The
    # recorded Imperial reply gave "1 lb flour" 454 g and "1/2 pint milk" 284 ml; such a pair adds
    # nothing and leaves the Weight and Volume pills idle, so it is dropped.
    def test_an_ai_conversion_of_the_same_kind_is_dropped(self):
        flour = _oil(amount=1.0, unit="lb", name="plain flour", converted_amount=454.0, converted_unit="g")
        milk = _oil(id="ing_03", amount=0.5, unit="pint", name="milk", converted_amount=284.0, converted_unit="ml")
        result, _ = _refine(_refinement(flour, milk))
        assert [(i.converted_amount, i.converted_unit, i.is_ai_converted) for i in result.structured_ingredients] == [(None, None, False)] * 2

    def test_an_ai_conversion_of_an_imperial_measure_into_the_other_kind_is_kept(self):
        cream = _oil(amount=1.0, unit="gill", name="cream", converted_amount=145.0, converted_unit="g")
        stone = _oil(id="ing_03", amount=1.0, unit="stone", name="potatoes", converted_amount=9000.0, converted_unit="ml")
        result, _ = _refine(_refinement(cream, stone))
        assert [i.converted_unit for i in result.structured_ingredients] == ["g", "ml"]

    def test_an_ai_conversion_is_kept_when_the_line_unit_is_not_a_known_measure(self):
        eggs = _oil(amount=3.0, unit="st", name="ägg", converted_amount=150.0, converted_unit="g")
        result, _ = _refine(_refinement(eggs))
        assert result.structured_ingredients[0].converted_unit == "g"

    def test_an_ai_conversion_in_cups_is_dropped(self):
        result, _ = _refine(_refinement(_oil(converted_amount=0.06, converted_unit="cups")))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (None, None, False)


class TestTheDetectedSystem:
    def test_evidence_the_recipe_writes_is_kept(self):
        result, _ = _refine(_refinement(_oil(), detected="US", evidence="1 ounce (about 1/3 packed cup)"))
        assert (result.source_uom_system_detected, result.source_uom_system_evidence) == ("US", "1 ounce (about 1/3 packed cup)")

    def test_quoted_and_recased_evidence_is_accepted(self):
        # Review Focus 2.
        result, _ = _refine(_refinement(_oil(), detected="US", evidence="“1 OUNCE (about 1/3 packed cup)”"))
        assert result.source_uom_system_detected == "US"

    def test_evidence_the_recipe_never_writes_nulls_both(self):
        result, _ = _refine(_refinement(_oil(), detected="AU", evidence="1 cup (250 ml)"))
        assert (result.source_uom_system_detected, result.source_uom_system_evidence) == (None, None)

    def test_the_host_is_evidence_when_it_was_given(self):
        result, _ = _refine(_refinement(_oil(), detected="AU", evidence="taste.com.au"), source_host="taste.com.au")
        assert result.source_uom_system_detected == "AU"

    def test_a_host_quote_with_no_host_given_nulls_both(self):
        result, _ = _refine(_refinement(_oil(), detected="AU", evidence="taste.com.au"))
        assert result.source_uom_system_detected is None

    def test_an_unknown_system_word_is_dropped_and_the_recipe_kept(self):
        # Review Focus 3: a word outside the five must not fail validation and lose the recipe.
        result, _ = _refine(_refinement(_oil(), detected="Metric", evidence="1 tablespoon olive oil"))
        assert result.title == "Spaghetti Carbonara"
        assert (result.source_uom_system_detected, result.source_uom_system_evidence) == (None, None)

    def test_a_system_without_evidence_is_dropped(self):
        result, _ = _refine(_refinement(_oil(), detected="US", evidence=None))
        assert result.source_uom_system_detected is None

    def test_the_host_reaches_the_model_call(self):
        _, fn = _refine(_refinement(_oil()), source_host="cooking.nytimes.com")
        assert fn.call_args.kwargs["source_host"] == "cooking.nytimes.com"

    def test_a_lower_case_system_is_written_canonically(self):
        # Final review M2.
        result, _ = _refine(_refinement(_oil(), detected=" imperial ", evidence="1 ounce (about 1/3 packed cup)"))
        assert result.source_uom_system_detected == "Imperial"
        result, _ = _refine(_refinement(_oil(), detected="au", evidence="taste.com.au"), source_host="taste.com.au")
        assert result.source_uom_system_detected == "AU"

    def test_a_dot_boundary_suffix_of_the_host_is_evidence(self):
        # Final review M3.
        for quote in (".com.au", "com.au", "taste.com.au", "www.taste.com.au", "TASTE.com.au"):
            result, _ = _refine(_refinement(_oil(), detected="AU", evidence=quote), source_host="www.taste.com.au")
            assert result.source_uom_system_detected == "AU", quote

    def test_a_host_substring_off_a_dot_boundary_is_not_evidence(self):
        for quote in ("aste.com", "aste.com.au", "om.au", "taste", "au."):
            result, _ = _refine(_refinement(_oil(), detected="AU", evidence=quote), source_host="taste.com.au")
            assert result.source_uom_system_detected is None, quote


# Fix Roadmap F-010 (RecipeParser#59's final review, ruling 7): the evidence guard took any substring of
# the recipe, so a quote of "cup" verified and a wrong system stuck, resizing every cup and spoon.
_EVIDENCE_RAW = RecipeExtraction(
    name="Pumpkin Scones",
    notes="Uses Australian standard measures.",
    ingredients=[
        "1 cup (250 ml) milk",
        "1 tbsp (20 ml) caster sugar",
        "1 stone potatoes",
        "2 teacups plain flour",
        "½ cup (120 g) butter",
        "1 cup(237ml) cream",
    ],
    directions=["Bake."],
)


class TestWhatCountsAsEvidence:
    def test_a_measure_every_system_writes_is_not_evidence(self):
        for quote in ("cup", "1 cup", "CUP", "1 tablespoon", "tablespoon", "1 ounce", "ounce"):
            result, _ = _refine(_refinement(_oil(), detected="US", evidence=quote))
            assert (result.source_uom_system_detected, result.source_uom_system_evidence) == (None, None), quote

    def test_grams_beside_a_cup_are_not_evidence(self):
        # The prompt's own rule: American writers print grams beside cups too.
        result, _ = _refine(_refinement(_oil(), detected="UK", evidence="cup (120 g)"), raw=_EVIDENCE_RAW)
        assert result.source_uom_system_detected is None

    def test_a_piece_of_a_word_is_not_evidence(self):
        for quote in ("ted pecorino", "live oil", "packed cu", "e oil"):
            result, _ = _refine(_refinement(_oil(), detected="US", evidence=quote))
            assert result.source_uom_system_detected is None, quote

    def test_the_evidence_the_prompt_names_is_still_kept(self):
        for detected, quote in (
            ("AU", "1 cup (250 ml)"), ("AU", "(250 ml)"), ("AU", "1 tbsp (20 ml)"), ("US", "(237ml)"),
            ("UK", "caster sugar"), ("UK", "plain flour"), ("AU", "Australian standard measures"),
            ("Imperial", "1 stone potatoes"), ("Imperial", "1 stone"), ("Imperial", "teacups"),
        ):
            result, _ = _refine(_refinement(_oil(), detected=detected, evidence=quote), raw=_EVIDENCE_RAW)
            assert (result.source_uom_system_detected, result.source_uom_system_evidence) == (detected, quote), quote
