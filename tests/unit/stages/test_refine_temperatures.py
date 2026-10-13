"""refine() keeps good temperature tokens and unwraps bad ones (Cayenne oven temperatures D15)."""
import logging
from unittest.mock import MagicMock, patch

from recipeparser.models import CayenneRefinement, RecipeExtraction, StructuredIngredient, TokenizedDirection

RAW = RecipeExtraction(
    name="Roast", ingredients=["1 cup flour"],
    directions=["Preheat the oven to 350°F.", "Roast until the centre reads 63°C."],
)
FLOUR = StructuredIngredient(id="ing_01", kind="ingredient", amount=1, unit="cup", name="flour",
                             fallback_string="1 cup flour", line_index=0)


def _run(steps):
    from recipeparser.core.stages.refine import refine
    refinement = CayenneRefinement(
        title="Roast", base_servings=4, structured_ingredients=[FLOUR],
        tokenized_directions=[TokenizedDirection(step=n, text=t) for n, t in enumerate(steps, 1)],
    )
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        return refine(RAW, client=MagicMock())


# Review focus 4: a temperature token must not reach _validate_fat_tokens as an unknown ingredient.
def test_a_good_temperature_token_survives_and_the_recipe_is_kept():
    result = _run(["Preheat the oven to {{temp:350F,oven|350°F}}.", "Roast until the centre reads {{temp:63C|63°C}}."])
    assert result.tokenized_directions[0].text == "Preheat the oven to {{temp:350F,oven|350°F}}."
    assert result.tokenized_directions[1].text == "Roast until the centre reads {{temp:63C|63°C}}."


def test_a_malformed_value_is_unwrapped_to_its_words(caplog):
    with caplog.at_level(logging.WARNING):
        result = _run(["Preheat the oven to {{temp:350X,oven|350°F}}.", "Roast until the centre reads 63°C."])
    assert result.tokenized_directions[0].text == "Preheat the oven to 350°F."
    assert "temperature token" in caplog.text


def test_words_the_recipe_does_not_write_are_unwrapped():
    result = _run(["Preheat the oven to {{temp:180C,oven|180°C}}.", "Roast until the centre reads 63°C."])
    assert result.tokenized_directions[0].text == "Preheat the oven to 180°C."


def test_an_out_of_range_value_is_unwrapped():
    result = _run(["Preheat the oven to {{temp:3500F,oven|350°F}}.", "Roast until the centre reads 63°C."])
    assert result.tokenized_directions[0].text == "Preheat the oven to 350°F."


def test_an_unknown_ingredient_id_still_raises():
    import pytest
    with pytest.raises(ValueError, match="unknown ingredient"):
        _run(["Add {{ing_09|sugar|all}}.", "Roast."])


def _run_with(raw_steps, steps):
    from recipeparser.core.stages.refine import refine
    raw = RecipeExtraction(name="Roast", ingredients=["1 cup flour"], directions=raw_steps)
    refinement = CayenneRefinement(
        title="Roast", base_servings=4, structured_ingredients=[FLOUR],
        tokenized_directions=[TokenizedDirection(step=n, text=t) for n, t in enumerate(steps, 1)],
    )
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        return refine(raw, client=MagicMock())


def test_words_whose_figure_is_not_the_value_are_unwrapped():
    result = _run_with(["Bake at 350°F."], ["Bake at {{temp:180C,oven|350°F}}."])
    assert result.tokenized_directions[0].text == "Bake at 350°F."


def test_words_whose_figure_matches_the_value_are_kept():
    result = _run_with(["Bake at 350°F."], ["Bake at {{temp:350F,oven|350°F}}."])
    assert result.tokenized_directions[0].text == "Bake at {{temp:350F,oven|350°F}}."


def test_a_gas_mark_and_a_fan_figure_count_as_the_value_figure():
    result = _run_with(["Bake at 160°C fan, gas mark 4."],
                       ["Bake at {{temp:gas4,oven,fan=160C|160°C fan, gas mark 4}}."])
    assert "{{temp:" in result.tokenized_directions[0].text


def test_words_without_digits_are_kept():
    result = _run_with(["Bake in a moderate oven."], ["Bake in a {{temp:180C,oven|moderate oven}}."])
    assert result.tokenized_directions[0].text == "Bake in a {{temp:180C,oven|moderate oven}}."


def test_a_bare_figure_takes_in_its_unit_words_and_a_range_end_is_unwrapped():
    from recipeparser.core.stages.refine import refine
    raw = RecipeExtraction(name="Roast", ingredients=["1 cup flour"],
                           directions=["Heat oven to 350 degrees.", "Heat to 500 to 550°F."])
    refinement = CayenneRefinement(
        title="Roast", base_servings=4, structured_ingredients=[FLOUR],
        tokenized_directions=[TokenizedDirection(step=1, text="Heat oven to {{temp:350F,oven|350}} degrees."),
                              TokenizedDirection(step=2, text="Heat to 500 to {{temp:550F,oven|550°F}}.")],
    )
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        result = refine(raw, client=MagicMock())
    assert result.tokenized_directions[0].text == "Heat oven to {{temp:350F,oven|350 degrees}}."
    assert result.tokenized_directions[1].text == "Heat to 500 to 550°F."
