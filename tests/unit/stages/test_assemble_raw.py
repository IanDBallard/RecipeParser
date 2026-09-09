"""assemble() carries raw lines and structured durations (spec 3.2, 3.6, 5.7)."""
from recipeparser.core.models import SourceMeta
from recipeparser.core.stages.assemble import assemble
from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection


def _refinement():
    return CayenneRefinement(
        title="Cake",
        base_servings=8,
        structured_ingredients=[
            StructuredIngredient(id="ing_01", amount=1.5, unit="cups", name="flour",
                                 fallback_string="1 1/2 cups flour", line_index=0),
        ],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
    )


def test_raw_lines_passed_through():
    r = assemble(_refinement(), [0.0] * 3, None, None, {},
                 ingredient_lines=["1 1/2 cups flour"], direction_steps=["Mix flour."])
    assert r.ingredient_lines == ["1 1/2 cups flour"]
    assert r.direction_steps == ["Mix flour."]


def test_raw_lines_derived_when_absent():
    r = assemble(_refinement(), [0.0] * 3, None, None, {})
    assert r.ingredient_lines == ["1 1/2 cups flour"]
    assert r.direction_steps == ["Mix flour."]


def test_durations_and_servings_parsed():
    r = assemble(_refinement(), [0.0] * 3, None, None, {},
                 prep_time="15 mins", cook_time="1-2 hours", servings_text="2-4")
    assert (r.prep_min_minutes, r.prep_max_minutes, r.prep_note) == (15, 15, None)
    assert (r.cook_min_minutes, r.cook_max_minutes) == (60, 120)
    assert (r.servings_min, r.servings_max) == (2, 4)
    assert r.base_servings == 2          # servings_min wins over REFINE's 8


def test_base_servings_falls_back_to_refinement():
    r = assemble(_refinement(), [0.0] * 3, None, None, {}, servings_text=None)
    assert r.base_servings == 8


def test_meta_time_wins_and_is_parsed():
    meta = SourceMeta(prep_time="45 min plus chilling")
    r = assemble(_refinement(), [0.0] * 3, None, None, {}, prep_time="10 mins", meta=meta)
    assert r.prep_time == "45 min plus chilling"
    assert (r.prep_min_minutes, r.prep_note) == (45, "plus chilling")
