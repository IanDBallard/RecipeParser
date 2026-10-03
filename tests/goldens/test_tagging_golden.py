"""The tagging golden set (Cayenne Fix Roadmap F-205): does the model follow the
TAGGING RULES on recipes that broke the old prompts?

The owner's library had potato gnocchi tagged Egg for the eggs in its dough and
minestrone tagged Chicken for its stock. Each case below asserts that kind of tag
is left off and, beside it, a tag the dish should get, so a model that tags
nothing cannot pass. The taxonomy spans six axes on purpose: the rules name no
axis and have to serve every one. Assertions are narrow: only the tag the case
exists for, not the whole answer, which is the model's judgement to make.

Recorded replies replay from tests/goldens/gemini/tagging/. Until they are
recorded the set skips. Record, and re-record after any change to the rules or
the categorisation prompt text, with:
    pytest tests/goldens/test_tagging_golden.py --record-gemini -n0
(a real GOOGLE_API_KEY in the shell; the suite's default dummy key refuses).
Six paid calls.
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from recipeparser.core.stages.categorize import categorize
from recipeparser.gemini import categorize_batch, refine_recipe_for_cayenne
from recipeparser.models import RecipeExtraction
from tests.goldens.paths import GEMINI_DIR

FIXTURE = "tagging"

AXES: Dict[str, List[str]] = {
    "Cuisine": ["Asian", "British", "French", "Indian", "Italian", "Thai"],
    "Protein": ["Beans & Lentils", "Beef", "Chicken", "Egg", "Fish & Seafood", "Pork", "Tofu"],
    "Course": ["Breakfast", "Dessert", "Main", "Side", "Starter"],
    "Diet": ["Gluten-free", "Vegan", "Vegetarian"],
    "Preparation Method": ["Braises, Stews & Curries", "Cakes", "Dumplings", "Pasta & Noodles", "Soups"],
    "Technique": ["Baking", "Braising", "Searing & Sautéing", "Simmering & Boiling"],
}
PARENTS = {"Thai": "Asian"}


@pytest.fixture(autouse=True)
def _recorded(record_gemini):
    if not record_gemini and not (GEMINI_DIR / FIXTURE).exists():
        pytest.skip(
            "The tagging golden set is not recorded yet. Record it with "
            "`pytest tests/goldens/test_tagging_golden.py --record-gemini -n0` and a real GOOGLE_API_KEY."
        )


GNOCCHI = RecipeExtraction(
    name="Potato Gnocchi",
    servings="4",
    ingredients=["1 kg floury potatoes", "2 eggs", "250 g plain flour", "1 tsp salt", "Pinch of nutmeg"],
    directions=[
        "Boil the potatoes in their skins until tender, then peel and rice them.",
        "Mix in the eggs, flour, salt and nutmeg to a soft dough.",
        "Roll into ropes, cut into 2 cm pieces and mark with a fork.",
        "Boil in batches until they float, about 2 minutes.",
    ],
)

SPONGE = RecipeExtraction(
    name="Victoria Sponge",
    servings="8",
    ingredients=["225 g butter, softened", "225 g caster sugar", "4 eggs", "225 g self-raising flour",
                 "4 tbsp raspberry jam", "150 ml double cream, whipped"],
    directions=[
        "Cream the butter and sugar, then beat in the eggs one at a time.",
        "Fold in the flour and divide between two lined 20 cm tins.",
        "Bake at 180C for 20-25 minutes. Cool.",
        "Sandwich with the jam and cream.",
    ],
)

MINESTRONE = RecipeExtraction(
    name="Minestrone",
    servings="6",
    ingredients=["2 tbsp olive oil", "1 onion, diced", "2 carrots, diced", "2 celery sticks, diced",
                 "2 garlic cloves", "400 g tin chopped tomatoes", "1.5 litres chicken stock",
                 "400 g tin cannellini beans", "100 g small pasta", "1 courgette, diced", "Parmesan, to serve"],
    directions=[
        "Soften the onion, carrot and celery in the oil, then add the garlic.",
        "Add the tomatoes and stock and simmer for 20 minutes.",
        "Add the beans, pasta and courgette and simmer until the pasta is tender.",
        "Serve with Parmesan.",
    ],
)

BRAISE = RecipeExtraction(
    name="Beef Braised in Red Wine",
    servings="6",
    ingredients=["1.5 kg beef shin, in large pieces", "2 tbsp oil", "2 onions, sliced", "2 carrots",
                 "750 ml red wine", "500 ml beef stock", "2 bay leaves", "Thyme"],
    directions=[
        "Sear the beef in the oil in batches until well browned.",
        "Soften the onions and carrots in the same pan.",
        "Return the beef, add the wine, stock and herbs, and bring to a simmer.",
        "Cover and cook in the oven at 150C for 3 hours, until the beef is tender.",
    ],
)

GREEN_CURRY = RecipeExtraction(
    name="Thai Green Chicken Curry",
    servings="4",
    ingredients=["3 tbsp green curry paste", "400 ml coconut milk", "600 g chicken thighs, sliced",
                 "1 tbsp fish sauce", "1 tsp palm sugar", "Thai basil", "4 kaffir lime leaves"],
    directions=[
        "Fry the curry paste in a little of the coconut milk until fragrant.",
        "Add the chicken and stir until sealed.",
        "Add the rest of the coconut milk, fish sauce, sugar and lime leaves; simmer 15 minutes.",
        "Finish with Thai basil.",
    ],
)


def _tags(golden_client, raw: RecipeExtraction) -> Dict[str, List[str]]:
    """The tags an ingest would write: the model's answer through CATEGORIZE."""
    refined = refine_recipe_for_cayenne(raw, golden_client(FIXTURE), user_axes=AXES, parents=PARENTS)
    assert refined is not None
    return categorize(refined, AXES, PARENTS)


def test_eggs_in_a_dough_do_not_make_gnocchi_an_egg_dish(golden_client):
    tags = _tags(golden_client, GNOCCHI)
    assert "Egg" not in tags.get("Protein", [])
    assert "Italian" in tags.get("Cuisine", [])


def test_eggs_in_a_cake_do_not_make_it_an_egg_dish(golden_client):
    tags = _tags(golden_client, SPONGE)
    assert "Egg" not in tags.get("Protein", [])
    assert "Cakes" in tags.get("Preparation Method", [])


def test_chicken_stock_makes_a_soup_neither_chicken_nor_vegetarian(golden_client):
    tags = _tags(golden_client, MINESTRONE)
    assert "Chicken" not in tags.get("Protein", [])
    assert not set(tags.get("Diet", [])) & {"Vegetarian", "Vegan"}
    assert "Soups" in tags.get("Preparation Method", [])


def test_searing_before_a_braise_does_not_name_the_technique(golden_client):
    tags = _tags(golden_client, BRAISE)
    assert "Searing & Sautéing" not in tags.get("Technique", [])
    assert "Braising" in tags.get("Technique", [])
    assert "Beef" in tags.get("Protein", [])


def test_a_dish_built_on_chicken_is_tagged_chicken_and_thai_not_asian(golden_client):
    # The positive control: the rules must not stop a main ingredient being tagged.
    tags = _tags(golden_client, GREEN_CURRY)
    assert "Chicken" in tags.get("Protein", [])
    assert "Thai" in tags.get("Cuisine", [])
    assert "Asian" not in tags.get("Cuisine", [])


def _row(rid: str, raw: RecipeExtraction) -> dict:
    return {"id": rid, "title": raw.name, "ingredient_lines": raw.ingredients, "direction_steps": raw.directions}


def test_a_bulk_recategorise_of_the_protein_axis_follows_the_same_rules(golden_client):
    # "Apply to existing recipes" on a whole axis: one call over several recipes.
    out = categorize_batch(
        [_row("gnocchi", GNOCCHI), _row("sponge", SPONGE), _row("minestrone", MINESTRONE),
         _row("curry", GREEN_CURRY)],
        {"Protein": AXES["Protein"]},
        golden_client(FIXTURE),
    )
    assert "Egg" not in out.get("gnocchi", [])
    assert "Egg" not in out.get("sponge", [])
    assert "Chicken" not in out.get("minestrone", [])
    assert "Chicken" in out.get("curry", [])
