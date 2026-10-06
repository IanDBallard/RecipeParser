"""The tagging golden set (Cayenne Fix Roadmap F-205, F-246): does the model follow
the TAGGING RULES on recipes that broke the old prompts?

The owner's library had potato gnocchi tagged Egg for the eggs in its dough and
minestrone tagged Chicken for its stock. Each case below asserts that kind of tag
is left off and, beside it, a tag the dish should get, so a model that tags
nothing cannot pass. The taxonomy spans six axes on purpose: the rules name no
axis and have to serve every one. Assertions are narrow: only the tag the case
exists for, not the whole answer, which is the model's judgement to make.

Since F-246 an import tags in its own TAG stage, one axis per call, and these
cases go through it: each recipe alone, as a single-recipe import tags it, so six
calls per recipe. REFINE no longer tags. The verbatim lines the TAG stage reads
are the extraction's own here, as REFINE copies them.

Recorded replies replay from tests/goldens/gemini/tagging/. Until they are
recorded the set skips. Record, and re-record after any change to the rules or
the categorisation prompt text, with:
    pytest tests/goldens/test_tagging_golden.py --record-gemini -n0
(a real GOOGLE_API_KEY in the shell; the suite's default dummy key refuses).
Thirty-one paid calls, on the cheapest tier. ``--record-gemini-missing`` records
only the calls that have no reply yet.

Before F-246 two of these cases could not pass on the pinned model: gnocchi and
the sponge were both tagged Egg for eggs worked into a dough or a batter when
REFINE offered every axis in one request, and they were strict xfails naming the
model. Asked one axis at a time the same model leaves Protein empty for both: the
recording of 2026-10-05 passed them, and the markers came off. Each case is still
split from the tag the same dish *must* get, so a model that tags nothing fails.

Read the minestrone with care: over five runs of the old import it claimed
Vegetarian in three. One axis per call it held in every run measured on
2026-10-05, but stock-based soups batched ten to a call beside vegetarian dishes
did take Vegetarian, which is why the TAG stage batches by five; a re-record may
still fail it. If it does, that is the model's
variance and not a regression in the rules -- check it against a measurement of
several runs before treating it as one.
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from recipeparser.core.stages.tag import tag_batch
from recipeparser.gemini import categorize_batch
from recipeparser.models import RecipeExtraction
from tests.goldens.golden_client import GoldenClient
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


class _Finished:
    """The fields the TAG stage reads from a finished recipe, and the two it writes."""

    def __init__(self, raw: RecipeExtraction) -> None:
        self.title = raw.name
        self.ingredient_lines = list(raw.ingredients)
        self.direction_steps = list(raw.directions)
        self.grid_categories: Dict[str, List[str]] = {}
        self.categories: List[str] = []


@pytest.fixture(scope="module")
def tags(request):
    """The tags an ingest would write, one TAG pass per recipe however many cases read it.

    Module-scoped and cached on purpose: a case split into "what it must be tagged" and
    "what it must not" would otherwise pay twice for one recipe when recording, and the
    two halves would then judge two different answers. The client is built here rather
    than taken from the ``golden_client`` factory because that fixture is
    function-scoped; a fresh client per recipe keeps each body's reply at ordinal 0.
    """
    record = bool(request.config.getoption("--record-gemini"))
    record_missing = bool(request.config.getoption("--record-gemini-missing"))
    answers: Dict[str, Dict[str, List[str]]] = {}

    def _for(name: str, raw: RecipeExtraction) -> Dict[str, List[str]]:
        if name not in answers:
            client = GoldenClient(fixture_id=FIXTURE, root=GEMINI_DIR, record=record, record_missing=record_missing)
            recipe = _Finished(raw)
            failures = tag_batch(
                [recipe], AXES, PARENTS,
                lambda rows, axes: categorize_batch(rows, axes, client, parents=PARENTS),
            )
            assert failures == []
            answers[name] = recipe.grid_categories
        return answers[name]

    return _for


def test_gnocchi_is_tagged_the_italian_pasta_dish_it_is(tags):
    # The positive half of the gnocchi case: the split below must not let a model that tags
    # nothing at all pass this set.
    assert "Italian" in tags("gnocchi", GNOCCHI).get("Cuisine", [])


def test_eggs_in_a_dough_do_not_make_gnocchi_an_egg_dish(tags):
    assert "Egg" not in tags("gnocchi", GNOCCHI).get("Protein", [])


def test_a_sponge_is_tagged_a_cake(tags):
    assert "Cakes" in tags("sponge", SPONGE).get("Preparation Method", [])


def test_eggs_in_a_cake_do_not_make_it_an_egg_dish(tags):
    assert "Egg" not in tags("sponge", SPONGE).get("Protein", [])


def test_chicken_stock_makes_a_soup_neither_chicken_nor_vegetarian(tags):
    # The least stable case under the old import, which claimed Vegetarian in three runs of
    # five. Asked one axis at a time it held in every measured run (2026-10-05), but see the
    # module docstring before calling a re-record failure a regression.
    minestrone = tags("minestrone", MINESTRONE)
    assert "Chicken" not in minestrone.get("Protein", [])
    assert not set(minestrone.get("Diet", [])) & {"Vegetarian", "Vegan"}
    assert "Soups" in minestrone.get("Preparation Method", [])


def test_searing_before_a_braise_does_not_name_the_technique(tags):
    braise = tags("braise", BRAISE)
    assert "Searing & Sautéing" not in braise.get("Technique", [])
    assert "Braising" in braise.get("Technique", [])
    assert "Beef" in braise.get("Protein", [])


def test_a_dish_built_on_chicken_is_tagged_chicken_and_thai_not_asian(tags):
    # The positive control: the rules must not stop a main ingredient being tagged.
    curry = tags("curry", GREEN_CURRY)
    assert "Chicken" in curry.get("Protein", [])
    assert "Thai" in curry.get("Cuisine", [])
    assert "Asian" not in curry.get("Cuisine", [])


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
