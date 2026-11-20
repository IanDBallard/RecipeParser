"""REFINE: the writer's two measures, AI conversions in g or ml, and a verified source system (D3, D5)."""
from unittest.mock import MagicMock, patch

import pytest

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
    notes="Measured with US cup measures.",
)


def _pecorino(**over) -> StructuredIngredient:
    fields = dict(
        id="ing_01", amount=1.0, unit="ounce", name="grated pecorino Romano",
        fallback_string=RAW.ingredients[0], converted_amount=0.333, converted_unit="cup",
        is_ai_converted=False, line_index=0,
    )
    fields.update(over)
    fields.setdefault("kind", "ingredient")
    return StructuredIngredient(**fields)


def _oil(**over) -> StructuredIngredient:
    fields = dict(
        id="ing_02", amount=1.0, unit="tablespoon", name="olive oil", fallback_string=RAW.ingredients[1],
        converted_amount=13.5, converted_unit="g", is_ai_converted=True, line_index=1,
    )
    fields.update(over)
    fields.setdefault("kind", "ingredient")
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


    # Fix Roadmap F-011 (RecipeParser#59's final review, ruling 7): the check matched the number only, so
    # a number the line writes under another unit passed as the writer's own second measure.
    def test_a_number_the_line_writes_under_another_unit_is_not_the_writers(self):
        line = "1 lb (450 g) potatoes, cut into 2 cm cubes"
        potatoes = _pecorino(unit="lb", fallback_string=line, converted_amount=2.0, converted_unit="cups")
        result, _ = _refine(_refinement(potatoes))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (None, None, False)

    def test_a_metric_measure_under_another_unit_is_re_marked_as_the_ais(self):
        line = "1 cup flour, baked at 180 C"
        flour = _pecorino(unit="cup", fallback_string=line, converted_amount=180.0, converted_unit="g")
        result, _ = _refine(_refinement(flour))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (180.0, "g", True)

    def test_the_writers_unit_may_differ_by_case_plural_or_full_stop(self):
        for line, amount, unit in (
            ("1 cup (240 mL) milk", 240.0, "ml"),
            ("250g/2 cups flour", 2.0, "cup"),
            ("4 oz. (1/2 cup) sugar", 0.5, "cups"),
            ("1 cup (4.5 oz) flour", 4.5, "oz."),
        ):
            ing = _pecorino(fallback_string=line, converted_amount=amount, converted_unit=unit)
            result, _ = _refine(_refinement(ing))
            got = result.structured_ingredients[0]
            assert (got.converted_amount, got.is_ai_converted) == (amount, False), line


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

    # Fix Roadmap F-684: a unit with no kind is a count ("3 st ägg" is three pieces), and the prompt
    # asks for no conversion on "3 eggs"; until then this test kept it.
    def test_an_ai_conversion_is_dropped_when_the_line_unit_is_not_a_known_measure(self):
        eggs = _oil(amount=3.0, unit="st", name="ägg", converted_amount=150.0, converted_unit="g")
        result, _ = _refine(_refinement(eggs))
        assert result.structured_ingredients[0].converted_unit is None

    def test_an_ai_conversion_in_cups_is_dropped(self):
        result, _ = _refine(_refinement(_oil(converted_amount=0.06, converted_unit="cups")))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (None, None, False)


class TestTheDetectedSystem:
    def test_evidence_the_recipe_writes_is_kept(self):
        result, _ = _refine(_refinement(_oil(), detected="US", evidence="US cup measures"))
        assert (result.source_uom_system_detected, result.source_uom_system_evidence) == ("US", "US cup measures")

    def test_quoted_and_recased_evidence_is_accepted(self):
        # Review Focus 2.
        result, _ = _refine(_refinement(_oil(), detected="US", evidence="“US CUP Measures”"))
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
        result, _ = _refine(_refinement(_oil(), detected=" imperial ", evidence="1 stone"), raw=_EVIDENCE_RAW)
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

    def test_a_host_is_evidence_only_for_the_system_the_prompt_gives_it(self):
        # Fix Roadmap F-131: the host verified ANY system, so "taste.com.au" offered for US stuck.
        # The prompt's map: .co.uk is UK, .com.au is AU, .co.nz is UK.
        for host, system in (("taste.com.au", "AU"), ("bbc.co.uk", "UK"), ("stuff.co.nz", "UK")):
            result, _ = _refine(_refinement(_oil(), detected=system, evidence=host), source_host=host)
            assert result.source_uom_system_detected == system, host
        for host, system in (
            ("taste.com.au", "US"), ("taste.com.au", "UK"), ("bbc.co.uk", "Imperial"), ("bbc.co.uk", "US"),
            ("stuff.co.nz", "AU"),
        ):
            result, _ = _refine(_refinement(_oil(), detected=system, evidence=host), source_host=host)
            assert (result.source_uom_system_detected, result.source_uom_system_evidence) == (None, None), (
                host, system)

    def test_a_port_or_a_trailing_dot_on_the_host_does_not_hide_its_system(self):
        # host_of keeps both ("bbc.co.uk:8080", "bbc.co.uk."); the map reads past them.
        for host in ("bbc.co.uk:8080", "bbc.co.uk.", "BBC.co.uk"):
            result, _ = _refine(_refinement(_oil(), detected="UK", evidence="co.uk"), source_host=host)
            assert result.source_uom_system_detected == "UK", host

    def test_a_host_the_prompt_does_not_map_is_evidence_of_nothing(self):
        # F-131: a .com site is read everywhere; the prompt names no system for it.
        for system in ("US", "UK", "EU", "AU", "Imperial"):
            result, _ = _refine(
                _refinement(_oil(), detected=system, evidence="cooking.nytimes.com"), source_host="cooking.nytimes.com"
            )
            assert result.source_uom_system_detected is None, system

    def test_a_quote_from_the_recipe_still_stands_whatever_the_host(self):
        # F-131 narrows the host path only: an American recipe on an Australian site is still US.
        result, _ = _refine(_refinement(_oil(), detected="Imperial", evidence="1 stone"), raw=_EVIDENCE_RAW,
                            source_host="taste.com.au")
        assert result.source_uom_system_detected == "Imperial"


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


# Fix Roadmap F-108 (batch 18): a quote of substance verified any of the five systems — "2 cups flour"
# passed as US, UK, EU, AU or Imperial alike, because an ingredient word counted as substance. The
# quote must now carry one of the prompt's kinds of evidence for the system it is offered for.
_SYSTEM_RAW = RecipeExtraction(
    name="Mixed Evidence",
    notes="Let us bake. Written for US cup measures; European metric measures differ.",
    ingredients=[
        "2 cups flour",
        "1 cup (250 ml) milk",
        "1 cup(237ml) cream",
        "1 tbsp (20 ml) caster sugar",
        "1 pint (568 ml) stock",
        "a 20 fl oz pint of beer",
        "2 cups all-purpose flour",
        "1 stone potatoes",
        "1 lb stone fruit",
        "1 gill cream",
    ],
    directions=["Bake."],
)


class TestEvidenceNamesItsSystem:
    def test_an_ingredient_line_is_evidence_of_no_system(self):
        for detected in ("US", "UK", "EU", "AU", "Imperial"):
            result, _ = _refine(_refinement(_oil(), detected=detected, evidence="2 cups flour"), raw=_SYSTEM_RAW)
            assert (result.source_uom_system_detected, result.source_uom_system_evidence) == (None, None), detected

    def test_evidence_offered_for_another_system_is_dropped(self):
        for detected, quote in (
            ("AU", "1 cup(237ml) cream"), ("US", "1 cup (250 ml)"), ("EU", "1 tbsp (20 ml)"),
            ("US", "caster sugar"), ("Imperial", "caster sugar"),
            ("UK", "1 stone potatoes"), ("AU", "2 cups all-purpose flour"), ("UK", "1 pint (568 ml)"),
            ("EU", "US cup measures"), ("Imperial", "1 cup (250 ml) milk"),
        ):
            result, _ = _refine(_refinement(_oil(), detected=detected, evidence=quote), raw=_SYSTEM_RAW)
            assert result.source_uom_system_detected is None, (detected, quote)

    def test_a_word_that_only_looks_like_evidence_is_not(self):
        for detected, quote in (("US", "let us bake"), ("Imperial", "stone fruit"), ("Imperial", "1 lb stone fruit")):
            result, _ = _refine(_refinement(_oil(), detected=detected, evidence=quote), raw=_SYSTEM_RAW)
            assert result.source_uom_system_detected is None, (detected, quote)

    def test_each_systems_own_evidence_is_kept(self):
        for detected, quote in (
            ("US", "(237ml)"), ("US", "2 cups all-purpose flour"), ("US", "US cup measures"),
            ("UK", "1 cup (250 ml)"), ("UK", "caster sugar"), ("EU", "(250 ml)"),
            ("EU", "European metric measures"), ("AU", "1 tbsp (20 ml)"), ("AU", "1 cup (250 ml) milk"),
            ("Imperial", "1 pint (568 ml)"), ("Imperial", "a 20 fl oz pint"), ("Imperial", "1 gill cream"),
            ("Imperial", "1 stone potatoes"),
        ):
            result, _ = _refine(_refinement(_oil(), detected=detected, evidence=quote), raw=_SYSTEM_RAW)
            assert (result.source_uom_system_detected, result.source_uom_system_evidence) == (detected, quote), quote

    def test_the_host_stays_evidence_for_the_system_it_is_offered_for(self):
        result, _ = _refine(_refinement(_oil(), detected="AU", evidence="taste.com.au"), source_host="taste.com.au",
                            raw=_SYSTEM_RAW)
        assert result.source_uom_system_detected == "AU"


# The prompt's fifth kind of evidence: ingredient names only one country uses. The review of batch 18
# widened the closed lists to the common unambiguous British/American pairs.
_NAMES_RAW = RecipeExtraction(
    name="Ratatouille",
    ingredients=[
        "2 courgettes", "1 aubergine", "1 tbsp icing sugar", "1 tsp bicarbonate of soda", "1 tbsp cornflour",
        "3 spring onions", "2 tbsp demerara sugar", "1 tbsp golden syrup",
        "2 zucchini", "1 eggplant", "1 bunch cilantro", "1 tbsp powdered sugar", "1 tsp baking soda",
        "1 tbsp cornstarch", "3 scallions", "1 bunch coriander",
    ],
    directions=["Cook."],
)
_UK_NAMES = ("courgettes", "aubergine", "icing sugar", "bicarbonate of soda", "cornflour", "spring onions",
             "demerara sugar", "golden syrup", "2 COURGETTES")
_US_NAMES = ("zucchini", "eggplant", "cilantro", "powdered sugar", "baking soda", "cornstarch", "scallions")


class TestRegionalIngredientNames:
    def test_a_british_name_is_uk_evidence(self):
        for quote in _UK_NAMES:
            result, _ = _refine(_refinement(_oil(), detected="UK", evidence=quote), raw=_NAMES_RAW)
            assert (result.source_uom_system_detected, result.source_uom_system_evidence) == ("UK", quote), quote

    def test_an_american_name_is_us_evidence(self):
        for quote in _US_NAMES:
            result, _ = _refine(_refinement(_oil(), detected="US", evidence=quote), raw=_NAMES_RAW)
            assert (result.source_uom_system_detected, result.source_uom_system_evidence) == ("US", quote), quote

    def test_a_name_is_evidence_of_its_own_country_only(self):
        for detected, names in (("US", _UK_NAMES), ("AU", _UK_NAMES), ("Imperial", _UK_NAMES),
                                ("UK", _US_NAMES), ("EU", _US_NAMES)):
            for quote in names:
                result, _ = _refine(_refinement(_oil(), detected=detected, evidence=quote), raw=_NAMES_RAW)
                assert result.source_uom_system_detected is None, (detected, quote)

    def test_a_shared_name_is_evidence_of_neither(self):
        for detected in ("UK", "US"):
            result, _ = _refine(_refinement(_oil(), detected=detected, evidence="1 bunch coriander"), raw=_NAMES_RAW)
            assert result.source_uom_system_detected is None, detected


def _line(id_, line, amount, unit, name, line_index=0, **over) -> StructuredIngredient:
    fields = dict(id=id_, amount=amount, unit=unit, name=name, fallback_string=line,
                  converted_amount=None, converted_unit=None, is_ai_converted=False,
                  line_index=line_index, kind="ingredient")
    fields.update(over)
    return StructuredIngredient(**fields)


def _raw(*lines) -> RecipeExtraction:
    return RecipeExtraction(name="Spaghetti Carbonara", ingredients=list(lines), directions=["Cook."])


class TestAConversionOnACount:
    """Fix Roadmap F-684: "2 limes, juiced, to get 2 1/2 tbsp" was kept as 2 limes ≈ 37.5 ml."""

    def test_an_ai_conversion_on_a_count_unit_is_dropped(self):
        line = "2 limes, juiced, to get 2 1/2 tbsp"
        limes = _line("ing_01", line, 2.0, "limes", "limes", converted_amount=37.5, converted_unit="ml",
                      is_ai_converted=True)
        result, _ = _refine(_refinement(limes), raw=_raw(line))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (None, None, False)

    def test_an_ai_conversion_on_a_line_with_no_unit_is_dropped(self):
        line = "3 eggs"
        eggs = _line("ing_01", line, 3.0, None, "eggs", converted_amount=150.0, converted_unit="g",
                     is_ai_converted=True)
        result, _ = _refine(_refinement(eggs), raw=_raw(line))
        assert result.structured_ingredients[0].converted_amount is None

    @pytest.mark.parametrize("unit", ["tbsps", "Tbsp.", "kilo", "gms", "fl. oz", "qts", "pound"])
    def test_an_ai_conversion_on_a_measure_spelled_any_registry_way_is_kept(self, unit):
        line = f"2 {unit} butter"
        butter = _line("ing_01", line, 2.0, unit, "butter", converted_amount=28.0, converted_unit="ml",
                       is_ai_converted=True)
        kind_of_unit = "weight" if unit in ("kilo", "gms", "pound") else "volume"
        if kind_of_unit == "volume":
            butter.converted_unit = "g"
        result, _ = _refine(_refinement(butter), raw=_raw(line))
        assert result.structured_ingredients[0].converted_amount == 28.0

    def test_a_writers_size_on_a_count_is_kept(self):
        line = "1 (14-ounce) can coconut milk"
        can = _line("ing_01", line, 1.0, "can", "coconut milk", converted_amount=14.0, converted_unit="ounce")
        result, _ = _refine(_refinement(can), raw=_raw(line))
        ing = result.structured_ingredients[0]
        assert (ing.converted_amount, ing.converted_unit, ing.is_ai_converted) == (14.0, "ounce", False)


class TestABracketedSizeAfterACount:
    """Fix Roadmap F-686: "1 (4- to 5-pound) brisket" was stored as 1, no unit: "Beef · 1"."""

    @pytest.mark.parametrize("line,amount,unit", [
        ("1 (4- to 5-pound) brisket", 5.0, "pound"),
        ("1 (4 to 5 lb) brisket", 5.0, "lb"),
        ("2 (14-ounce) cans chickpeas, drained", 14.0, "ounce"),
        ("1 (400g) tin chopped tomatoes", 400.0, "g"),
        ("1 (2 1/2-pound) chicken", 2.5, "pound"),
        ("1 (750 ml) bottle red wine", 750.0, "ml"),
    ])
    def test_the_bracketed_size_becomes_the_writers_second_measure(self, line, amount, unit):
        ing = _line("ing_01", line, float(line.split()[0]), None, "thing")
        result, _ = _refine(_refinement(ing), raw=_raw(line))
        got = result.structured_ingredients[0]
        assert (got.converted_amount, got.converted_unit, got.is_ai_converted) == (amount, unit, False)

    def test_a_size_the_model_misspelt_is_replaced_by_the_lines_own(self):
        # "oz" is not in the line, so the check re-marks it as the AI's and drops it (not g or ml);
        # the bracket then gives the writer's own word.
        line = "1 (14-ounce) can coconut milk"
        can = _line("ing_01", line, 1.0, "can", "coconut milk", converted_amount=14.0, converted_unit="oz")
        result, _ = _refine(_refinement(can), raw=_raw(line))
        got = result.structured_ingredients[0]
        assert (got.converted_amount, got.converted_unit, got.is_ai_converted) == (14.0, "ounce", False)

    def test_an_ai_guess_at_the_size_gives_way_to_the_brackets(self):
        line = "1 (14-ounce) can coconut milk"
        can = _line("ing_01", line, 1.0, "can", "coconut milk", converted_amount=400.0, converted_unit="ml",
                    is_ai_converted=True)
        result, _ = _refine(_refinement(can), raw=_raw(line))
        got = result.structured_ingredients[0]
        assert (got.converted_amount, got.converted_unit, got.is_ai_converted) == (14.0, "ounce", False)

    @pytest.mark.parametrize("line,unit", [
        ("1 (optional) bay leaf", None),
        ("2 cups (about 3 medium) onions", "cups"),
        ("1 (2-inch) piece ginger", None),
        ("3 (about) eggs", None),
    ])
    def test_a_bracket_that_is_not_a_size_after_a_count_is_ignored(self, line, unit):
        ing = _line("ing_01", line, float(line.split()[0]), unit, "thing")
        result, _ = _refine(_refinement(ing), raw=_raw(line))
        assert result.structured_ingredients[0].converted_amount is None


class TestAnExtraAmount:
    """Fix Roadmap F-685: "60g coconut cream, plus 100g extra for the coriander cream" was stored as 60 g."""

    def test_an_extra_amount_in_the_same_unit_is_added_to_the_line(self):
        line = "60g coconut cream, plus 100g extra for the coriander cream"
        cream = _line("ing_01", line, 60.0, "g", "coconut cream")
        result, _ = _refine(_refinement(cream), raw=_raw(line))
        assert result.structured_ingredients[0].amount == 160.0

    def test_the_parts_in_the_method_then_add_up(self):
        line = "60g coconut cream, plus 100g extra for the coriander cream"
        cream = _line("ing_01", line, 60.0, "g", "coconut cream")
        refinement = _refinement(cream)
        refinement.tokenized_directions = [
            TokenizedDirection(step=1, text="Stir in {{ing_01|60g|60 g}} coconut cream."),
            TokenizedDirection(step=2, text="Whisk {{ing_01|100g|100 g}} coconut cream with the coriander."),
        ]
        result, _ = _refine(refinement, raw=_raw(line))
        assert [d.text for d in result.tokenized_directions] == [d.text for d in refinement.tokenized_directions]

    @pytest.mark.parametrize("line,amount,unit,expected", [
        ("2 tbsp olive oil, plus 1 tbsp to serve", 2.0, "tbsp", 3.0),
        ("1 cup sugar plus 2 tablespoons", 1.0, "cup", 1.0),
        ("1 1/2 cups flour, plus extra for dusting", 1.5, "cups", 1.5),
        ("100g butter, plus 25 grams more", 100.0, "g", 125.0),
        ("2 lemons, plus 1 to serve", 2.0, None, 3.0),
    ])
    def test_only_a_figure_in_the_lines_own_unit_is_added(self, line, amount, unit, expected):
        ing = _line("ing_01", line, amount, unit, "thing")
        result, _ = _refine(_refinement(ing), raw=_raw(line))
        assert result.structured_ingredients[0].amount == expected

    def test_an_ai_conversion_grows_with_the_amount(self):
        line = "2 tbsp olive oil, plus 1 tbsp to serve"
        oil = _line("ing_01", line, 2.0, "tbsp", "olive oil", converted_amount=27.0, converted_unit="g",
                    is_ai_converted=True)
        result, _ = _refine(_refinement(oil), raw=_raw(line))
        got = result.structured_ingredients[0]
        assert (got.amount, got.converted_amount, got.converted_unit) == (3.0, 40.5, "g")

    def test_a_count_plus_another_food_is_not_added(self):
        line = "2 eggs, plus 1 yolk"
        eggs = _line("ing_01", line, 2.0, None, "eggs")
        result, _ = _refine(_refinement(eggs), raw=_raw(line))
        assert result.structured_ingredients[0].amount == 2.0
