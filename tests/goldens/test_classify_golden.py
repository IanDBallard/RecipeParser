"""The classify golden set (shopping design, *Testing*): naming consistency
with known_foods, the *Evidence* cases, and count estimates within a tolerance.

Recorded replies replay from tests/goldens/gemini/shopping-classify/. The
owner approved the paid recording 2026-10-02, re-record after any prompt
change with:  pytest tests/goldens/test_classify_golden.py --record-gemini
(a real GOOGLE_API_KEY in the shell; the suite's default dummy key refuses).

Tolerances, not equalities, for counts: the model estimates; the device sums
and rounds. A naming assertion is exact — naming drift is the defect D4 moved
classification to Generate to avoid.
"""
from __future__ import annotations

import pytest

from recipeparser.shopping import ClassifyIngredient, classify_ingredients
from tests.goldens.golden_client import MissingRecordingError

FIXTURE = "shopping-classify"


def ing(key: str, text: str, name: str, amount=None, unit=None) -> ClassifyIngredient:
    return ClassifyIngredient(key=key, text=text, name=name, amount=amount, unit=unit)


def by_key(items):
    return {i.key: i for i in items}


def test_garlic_two_writings_one_food(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "4 garlic cloves", "garlic cloves", 4.0),
        ing("b", "2 cloves garlic, minced", "garlic", 2.0),
    ], []))
    assert items["a"].food == items["b"].food
    assert "garlic" in items["a"].food


def test_onion_whole_and_chopped_one_food(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "1 onion, sliced", "onion", 1.0),
        ing("b", "1 cup chopped onion", "onion", 1.0, "cup"),
    ], []))
    assert items["a"].food == "onion"
    assert items["b"].food == "onion"
    assert items["a"].count is not None and 0.5 <= items["a"].count <= 2.0
    assert items["b"].count is not None and 0.25 <= items["b"].count <= 2.0
    assert items["a"].count_unit == items["b"].count_unit


def test_butter_is_bought_by_weight(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "1 cup butter", "butter", 1.0, "cup"),
        ing("b", "3 tbsp butter", "butter", 3.0, "tbsp"),
    ], []))
    assert items["a"].food == "butter" == items["b"].food
    assert items["a"].count is None and items["b"].count is None


def test_salt_to_taste_is_pantry(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "salt to taste", "salt"),
    ], []))
    assert items["a"].food == "salt"
    assert items["a"].pantry is True
    assert items["a"].aisle == "spices"


def test_eggs_singular_and_counted(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "2 eggs", "eggs", 2.0),
        ing("b", "1 egg, beaten", "egg", 1.0),
        ing("c", "2 free-range eggs", "free-range eggs", 2.0),
    ], []))
    assert items["a"].food == "egg" == items["b"].food == items["c"].food
    assert items["a"].count is not None and 1.5 <= items["a"].count <= 2.5
    assert items["b"].count is not None and 0.5 <= items["b"].count <= 1.5


def test_a_known_food_is_reused_verbatim(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "3 scallions, sliced", "scallions", 3.0),
    ], ["spring onion", "flour"]))
    assert items["a"].food == "spring onion"


def test_a_variety_that_changes_the_buy_stays(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "1 red onion", "red onion", 1.0),
        ing("b", "1 diced onion", "diced onion", 1.0),
    ], []))
    assert items["a"].food == "red onion"
    assert items["b"].food == "onion"


def test_functionally_distinct_products_never_collapse(golden_client):
    """A near-twin on KNOWN FOODS must not swallow a chemically or functionally
    different product (the owner's guardrail, 2026-10-03): leavening and canned
    milks are the classic collapses. Finishing salt was the third until F-891:
    batch 87 folds flaky sea salt into salt (owner-approved 2026-11-27), so its
    case is judged by test_grade_and_origin_words_fold_into_the_base_food. The
    ingredient stays in the request so the recording's key does not move."""
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "1 tsp baking powder", "baking powder", 1.0, "tsp"),
        ing("b", "1 cup evaporated milk", "evaporated milk", 1.0, "cup"),
        ing("c", "flaky sea salt, to finish", "flaky sea salt"),
    ], ["baking soda", "sweetened condensed milk", "salt"]))
    assert items["a"].food == "baking powder"
    assert items["b"].food == "evaporated milk"
    assert "c" in items


def test_a_tin_and_the_aisles(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "1 x 400g tin chopped tomatoes", "chopped tomatoes", 1.0, "tin"),
        ing("b", "500g plain flour", "plain flour", 500.0, "g"),
        ing("c", "200ml crème fraîche", "crème fraîche", 200.0, "ml"),
    ], []))
    assert items["a"].aisle == "tins_jars"
    assert items["a"].count is not None and 0.5 <= items["a"].count <= 1.5
    assert items["b"].aisle == "dry_goods" and items["b"].count is None
    assert items["c"].aisle == "dairy_eggs"
    assert items["c"].food == "crème fraîche"


def test_a_leftover_and_a_component_are_made(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "2 cups leftover roast turkey, shredded", "leftover roast turkey", 2.0, "cup"),
        ing("b", "1 cup chocolate ganache", "chocolate ganache", 1.0, "cup"),
        ing("c", "200g butter", "butter", 200.0, "g"),
    ], []))
    assert items["a"].made is True
    assert items["b"].made is True
    assert items["c"].made is False
    assert items["a"].food and items["a"].aisle


def test_a_generic_known_food_is_not_answered_for_a_specific_one(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "150g cheddar, grated", "cheddar", 150.0, "g"),
        ing("b", "500g bread flour", "bread flour", 500.0, "g"),
    ], ["cheese", "flour"]))
    assert items["a"].food == "cheddar"
    assert items["b"].food == "bread flour"


def test_a_bought_food_cooked_or_prepared_is_not_made(golden_client):
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "2 cups cooked rice", "cooked rice", 2.0, "cup"),
        ing("b", "500 ml chicken stock (homemade or shop-bought)", "chicken stock", 500.0, "ml"),
        ing("c", "50 g melted butter", "melted butter", 50.0, "g"),
    ], []))
    assert items["a"].made is False
    assert items["b"].made is False
    assert items["c"].made is False


def _classify_or_skip(client, ingredients, known):
    """F-891's sets are new work units: until a run with a real key records
    them (``--record-gemini-missing``, two paid calls) they skip, as the
    tagging set did (9.x precedent in CHANGELOG)."""
    try:
        return by_key(classify_ingredients(client, ingredients, known))
    except MissingRecordingError:
        pytest.skip(
            "F-891's classify golden is not recorded yet. Record it with "
            "`pytest tests/goldens/test_classify_golden.py --record-gemini-missing -n0` "
            "and a real GOOGLE_API_KEY."
        )


def test_grade_and_origin_words_fold_into_the_base_food(golden_client):
    """F-891: a word that only names a grade, grain, origin or brand of one
    base food is dropped, for variants Cayenne's table does not list too."""
    items = _classify_or_skip(golden_client(FIXTURE), [
        ing("a", "1 tsp kosher salt", "kosher salt", 1.0, "tsp"),
        ing("b", "Maldon salt, to finish", "Maldon salt"),
        ing("c", "flaky sea salt, to finish", "flaky sea salt"),
        ing("d", "1 tsp Diamond Crystal kosher salt", "Diamond Crystal kosher salt", 1.0, "tsp"),
        ing("e", "200 g granulated sugar", "granulated sugar", 200.0, "g"),
        ing("f", "freshly ground black pepper", "freshly ground black pepper"),
        ing("g", "1/2 tsp fine sea salt", "fine sea salt", 0.5, "tsp"),
    ], [])
    for key in "abcdg":
        assert items[key].food == "salt", key
    assert items["e"].food == "sugar"
    assert items["f"].food == "black pepper"


def test_a_word_that_makes_another_purchase_stays(golden_client):
    """F-891's other half, and the foods a word-level rule would wrongly fold:
    same-aisle purchases, grade words that name another food, and origins that
    name a style sold apart."""
    items = _classify_or_skip(golden_client(FIXTURE), [
        ing("a", "1 tsp celery salt", "celery salt", 1.0, "tsp"),
        ing("b", "2 tsp smoked paprika", "smoked paprika", 2.0, "tsp"),
        ing("c", "100 g caster sugar", "caster sugar", 100.0, "g"),
        ing("d", "2 sea bass fillets", "sea bass fillets", 2.0),
        ing("e", "320 g ready-rolled flaky pastry", "flaky pastry", 320.0, "g"),
        ing("f", "1 tsp granulated garlic", "granulated garlic", 1.0, "tsp"),
        ing("g", "1 tbsp Dijon mustard", "Dijon mustard", 1.0, "tbsp"),
        ing("h", "200 g Greek yogurt", "Greek yogurt", 200.0, "g"),
        ing("i", "1/2 tsp ground white pepper", "ground white pepper", 0.5, "tsp"),
    ], ["salt", "paprika", "sugar", "pastry", "garlic", "mustard", "yogurt"])
    assert items["a"].food == "celery salt"
    assert items["b"].food == "smoked paprika"
    assert items["c"].food == "caster sugar"
    assert items["d"].food == "sea bass"
    assert items["e"].food == "flaky pastry"
    assert items["f"].food == "granulated garlic"
    assert items["g"].food == "dijon mustard"
    assert items["h"].food == "greek yogurt"
    assert items["i"].food == "white pepper"
