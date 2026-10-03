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

from recipeparser.shopping import ClassifyIngredient, classify_ingredients

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
    different product (the owner's guardrail, 2026-10-03): leavening, canned
    milks and finishing salt are the classic collapses."""
    items = by_key(classify_ingredients(golden_client(FIXTURE), [
        ing("a", "1 tsp baking powder", "baking powder", 1.0, "tsp"),
        ing("b", "1 cup evaporated milk", "evaporated milk", 1.0, "cup"),
        ing("c", "flaky sea salt, to finish", "flaky sea salt"),
    ], ["baking soda", "sweetened condensed milk", "salt"]))
    assert items["a"].food == "baking powder"
    assert items["b"].food == "evaporated milk"
    assert items["c"].food != "salt"


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
