"""Measure the TAG stage (Cayenne Fix Roadmap F-246) on a fourteen-recipe sample.

The 2026-10-03 harness behind the retag's 105/105 was a throwaway and is lost. This
rebuilds a sample from F-205's record of it -- the egg, stock and technique failure
classes plus positive controls, using the dishes it names -- so its score is not
directly comparable with 105/105. It runs both import shapes: each recipe alone, as
a single-recipe import tags it, and all fourteen in batches of TAG_BATCH_SIZE, as a book does.
Re-run it when the model or the tagging prompt changes. Paid calls: 84 per run alone,
12 per run batched, on the cheapest tier. Needs a real GOOGLE_API_KEY:

    python scripts/measure_per_axis_tagging.py [runs] [batch size]

Given a batch size, it runs only the batched shape, at that size.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google import genai  # noqa: E402

from recipeparser.core.stages.tag import TAG_BATCH_SIZE, tag_batch  # noqa: E402
from recipeparser.gemini import categorize_batch  # noqa: E402

AXES: Dict[str, List[str]] = {
    "Cuisine": ["Asian", "British", "French", "Indian", "Italian", "Thai", "American", "Austrian"],
    "Protein": ["Beans & Lentils", "Beef", "Chicken", "Egg", "Fish & Seafood", "Pork", "Tofu"],
    "Course": ["Breakfast", "Dessert", "Main", "Side", "Starter"],
    "Diet": ["Gluten-free", "Vegan", "Vegetarian"],
    "Preparation Method": [
        "Braises, Stews & Curries",
        "Cakes",
        "Dumplings",
        "Pasta & Noodles",
        "Soups",
        "Rice & Risotto",
        "Pancakes & Waffles",
    ],
    "Technique": [
        "Baking",
        "Braising",
        "Deep-frying",
        "Searing & Sautéing",
        "Simmering & Boiling",
        "Roasting",
        "Shallow-frying",
    ],
}
PARENTS = {"Thai": "Asian"}
MEAT_FREE = {"Vegetarian", "Vegan"}


class R:
    def __init__(self, title, ingredients, directions, must=(), must_not=()):
        self.title = title
        self.ingredient_lines = ingredients
        self.direction_steps = directions
        self.must = must  # (axis, tag)
        self.must_not = must_not  # (axis, tag) ; tag "MEAT_FREE" = neither Vegetarian nor Vegan
        self.grid_categories: Dict[str, List[str]] = {}
        self.categories: List[str] = []


def sample() -> List[R]:
    return [
        # Egg class: eggs that bind or enrich.
        R(
            "Potato Gnocchi",
            ["1 kg floury potatoes", "2 eggs", "250 g plain flour", "1 tsp salt", "Pinch of nutmeg"],
            [
                "Boil the potatoes in their skins until tender, then peel and rice them.",
                "Mix in the eggs, flour, salt and nutmeg to a soft dough.",
                "Roll into ropes, cut into 2 cm pieces and mark with a fork.",
                "Boil in batches until they float, about 2 minutes.",
            ],
            must=[("Cuisine", "Italian")],
            must_not=[("Protein", "Egg")],
        ),
        R(
            "Victoria Sponge",
            [
                "225 g butter, softened",
                "225 g caster sugar",
                "4 eggs",
                "225 g self-raising flour",
                "4 tbsp raspberry jam",
                "150 ml double cream, whipped",
            ],
            [
                "Cream the butter and sugar, then beat in the eggs one at a time.",
                "Fold in the flour and divide between two lined 20 cm tins.",
                "Bake at 180C for 20-25 minutes. Cool.",
                "Sandwich with the jam and cream.",
            ],
            must=[("Preparation Method", "Cakes")],
            must_not=[("Protein", "Egg")],
        ),
        R(
            "Buttermilk Pancakes",
            [
                "200 g plain flour",
                "1 tsp baking powder",
                "1 tbsp sugar",
                "2 eggs",
                "300 ml buttermilk",
                "30 g butter, melted",
            ],
            [
                "Whisk the dry ingredients together.",
                "Beat the eggs with the buttermilk and butter and stir into the flour.",
                "Cook ladlefuls in a hot greased pan until bubbles form, then flip and cook 1 minute more.",
            ],
            must=[("Course", "Breakfast")],
            must_not=[("Protein", "Egg")],
        ),
        R(
            "Lemon Pound Cake",
            ["250 g butter", "250 g sugar", "4 eggs", "250 g plain flour", "Zest of 2 lemons"],
            [
                "Cream the butter and sugar.",
                "Beat in the eggs, then fold in the flour and zest.",
                "Bake in a loaf tin at 170C for 55 minutes.",
            ],
            must=[("Preparation Method", "Cakes")],
            must_not=[("Protein", "Egg")],
        ),
        # Stock class: a stock or sauce does not make the dish.
        R(
            "Minestrone",
            [
                "2 tbsp olive oil",
                "1 onion, diced",
                "2 carrots, diced",
                "2 celery sticks, diced",
                "2 garlic cloves",
                "400 g tin chopped tomatoes",
                "1.5 litres chicken stock",
                "400 g tin cannellini beans",
                "100 g small pasta",
                "1 courgette, diced",
                "Parmesan, to serve",
            ],
            [
                "Soften the onion, carrot and celery in the oil, then add the garlic.",
                "Add the tomatoes and stock and simmer for 20 minutes.",
                "Add the beans, pasta and courgette and simmer until the pasta is tender.",
                "Serve with Parmesan.",
            ],
            must=[("Preparation Method", "Soups")],
            must_not=[("Protein", "Chicken"), ("Diet", "MEAT_FREE")],
        ),
        R(
            "French Onion Soup",
            [
                "1 kg onions, thinly sliced",
                "50 g butter",
                "1.5 litres beef stock",
                "150 ml dry white wine",
                "1 baguette, sliced",
                "150 g Gruyère, grated",
            ],
            [
                "Cook the onions slowly in the butter for 45 minutes until deep brown.",
                "Add the wine and stock and simmer for 20 minutes.",
                "Ladle into bowls, top with toasted baguette and cheese and grill until bubbling.",
            ],
            must=[("Preparation Method", "Soups"), ("Cuisine", "French")],
            must_not=[("Protein", "Beef"), ("Diet", "MEAT_FREE")],
        ),
        R(
            "Risotto alla Milanese",
            [
                "300 g arborio rice",
                "1 onion, finely chopped",
                "60 g butter",
                "1.2 litres chicken stock",
                "Pinch of saffron",
                "100 ml white wine",
                "60 g Parmesan, grated",
            ],
            [
                "Soften the onion in half the butter.",
                "Add the rice and stir for a minute, then the wine.",
                "Add the hot stock and saffron a ladle at a time, stirring, for 18 minutes.",
                "Beat in the rest of the butter and the Parmesan.",
            ],
            must=[("Cuisine", "Italian")],
            must_not=[("Protein", "Chicken"), ("Diet", "MEAT_FREE")],
        ),
        R(
            "Spring Vegetable Soup",
            [
                "2 tbsp olive oil",
                "1 leek, sliced",
                "2 potatoes, diced",
                "1 litre vegetable stock",
                "150 g peas",
                "100 g asparagus",
            ],
            [
                "Soften the leek in the oil.",
                "Add the potatoes and stock and simmer 15 minutes.",
                "Add the peas and asparagus and simmer 5 minutes more.",
            ],
            must=[("Preparation Method", "Soups"), ("Diet", "MEAT_FREE")],
        ),
        # Technique class: a step on the way does not name the technique.
        R(
            "Beef Braised in Red Wine",
            [
                "1.5 kg beef shin, in large pieces",
                "2 tbsp oil",
                "2 onions, sliced",
                "2 carrots",
                "750 ml red wine",
                "500 ml beef stock",
                "2 bay leaves",
                "Thyme",
            ],
            [
                "Sear the beef in the oil in batches until well browned.",
                "Soften the onions and carrots in the same pan.",
                "Return the beef, add the wine, stock and herbs, and bring to a simmer.",
                "Cover and cook in the oven at 150C for 3 hours, until the beef is tender.",
            ],
            must=[("Technique", "Braising"), ("Protein", "Beef")],
            must_not=[("Technique", "Searing & Sautéing")],
        ),
        R(
            "Baked Ziti",
            ["400 g ziti", "700 g tomato sauce", "250 g ricotta", "200 g mozzarella", "50 g Parmesan"],
            [
                "Boil the pasta for 2 minutes less than the packet says and drain.",
                "Mix with the sauce and ricotta and spread in a baking dish.",
                "Top with the mozzarella and Parmesan and bake at 200C for 25 minutes.",
            ],
            must=[("Technique", "Baking")],
            must_not=[("Technique", "Simmering & Boiling")],
        ),
        # Positive controls.
        R(
            "Thai Green Chicken Curry",
            [
                "3 tbsp green curry paste",
                "400 ml coconut milk",
                "600 g chicken thighs, sliced",
                "1 tbsp fish sauce",
                "1 tsp palm sugar",
                "Thai basil",
                "4 kaffir lime leaves",
            ],
            [
                "Fry the curry paste in a little of the coconut milk until fragrant.",
                "Add the chicken and stir until sealed.",
                "Add the rest of the coconut milk, fish sauce, sugar and lime leaves; simmer 15 minutes.",
                "Finish with Thai basil.",
            ],
            must=[("Protein", "Chicken"), ("Cuisine", "Thai")],
            must_not=[("Cuisine", "Asian")],
        ),
        R(
            "Chicken Schnitzel",
            [
                "4 chicken breasts, flattened",
                "100 g plain flour",
                "2 eggs, beaten",
                "150 g breadcrumbs",
                "Oil for frying",
                "Lemon wedges",
            ],
            [
                "Dip each breast in flour, then egg, then breadcrumbs.",
                "Shallow-fry in hot oil for 3 minutes a side until golden and cooked through.",
                "Serve with lemon.",
            ],
            must=[("Protein", "Chicken")],
            must_not=[("Protein", "Egg")],
        ),
        R(
            "Beef Meatballs in Tomato Sauce",
            ["500 g minced beef", "1 egg", "50 g breadcrumbs", "1 garlic clove", "700 g passata", "Basil"],
            [
                "Mix the beef, egg, breadcrumbs and garlic and roll into 20 balls.",
                "Brown the meatballs in a little oil.",
                "Add the passata and simmer 20 minutes.",
                "Finish with basil.",
            ],
            must=[("Protein", "Beef")],
            must_not=[("Protein", "Egg")],
        ),
        R(
            "Shakshuka",
            ["2 tbsp olive oil", "1 onion", "2 red peppers", "2 tsp cumin", "800 g chopped tomatoes", "6 eggs"],
            [
                "Soften the onion and peppers in the oil with the cumin.",
                "Add the tomatoes and simmer 10 minutes.",
                "Make six wells, crack in the eggs, cover and cook until the whites set.",
            ],
            must=[("Protein", "Egg")],
        ),
    ]


def score(recipes: List[R]):
    passed, failed = 0, []
    for r in recipes:
        g = r.grid_categories
        for axis, tag in r.must:
            ok = bool(set(g.get(axis, [])) & MEAT_FREE) if tag == "MEAT_FREE" else tag in g.get(axis, [])
            passed += ok
            if not ok:
                failed.append(f"{r.title}: missing {axis}={tag} (got {g.get(axis, [])})")
        for axis, tag in r.must_not:
            ok = (not set(g.get(axis, [])) & MEAT_FREE) if tag == "MEAT_FREE" else tag not in g.get(axis, [])
            passed += ok
            if not ok:
                failed.append(f"{r.title}: has {axis}={tag} (got {g.get(axis, [])})")
    total = sum(len(r.must) + len(r.must_not) for r in recipes)
    return passed, total, failed


def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    size = int(sys.argv[2]) if len(sys.argv) > 2 else TAG_BATCH_SIZE
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    cat = lambda rows, axes: categorize_batch(rows, axes, client, parents=PARENTS)  # noqa: E731
    for shape in ("batched",) if len(sys.argv) > 2 else ("alone", "batched"):
        grand_p = grand_t = 0
        for run in range(runs):
            recipes = sample()
            fails = []
            if shape == "alone":
                for r in recipes:
                    fails += tag_batch([r], AXES, PARENTS, cat)
            else:
                for i in range(0, len(recipes), size):
                    fails += tag_batch(recipes[i : i + size], AXES, PARENTS, cat)
            p, t, failed = score(recipes)
            grand_p += p
            grand_t += t
            print(f"[{shape} run {run + 1}] {p}/{t}" + (f"  axis failures: {fails}" if fails else ""))
            for f in failed:
                print("   ", f)
            if run == 0:
                for r in recipes:
                    print(f"      {r.title}: {r.grid_categories}")
        print(f"== {shape}: {grand_p}/{grand_t}\n")


if __name__ == "__main__":
    main()
