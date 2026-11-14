"""recipe_text and source_citation: the chunk text and citation every library reader builds."""
from __future__ import annotations

from recipeparser.core.citation import classify_source, web_citation
from recipeparser.io.readers._recipe_text import recipe_text, source_citation


def test_recipe_text_without_facts_is_title_ingredients_directions():
    assert recipe_text("Pie", "1 egg", "Bake.") == "Pie\n\nIngredients:\n1 egg\n\nDirections:\nBake."


def test_recipe_text_puts_each_fact_on_its_own_line_before_the_ingredients():
    text = recipe_text("Pie", "1 egg", "Bake.", facts=(("Servings", "4"), ("Total time", "1 hr")))
    assert text == "Pie\n\nServings: 4\nTotal time: 1 hr\n\nIngredients:\n1 egg\n\nDirections:\nBake."


def test_recipe_text_drops_an_empty_fact():
    text = recipe_text("Pie", "1 egg", "Bake.", facts=(("Servings", ""), ("Total time", "1 hr")))
    assert text == "Pie\n\nTotal time: 1 hr\n\nIngredients:\n1 egg\n\nDirections:\nBake."


def test_recipe_text_with_one_servings_fact_matches_paprikas_historic_layout():
    """The exact string PaprikaReader built before the helper existed."""
    name, servings, ingredients, directions = "Pie", "Serves 4", "1 egg", "Bake."
    historic = f"{name}\n\nServings: {servings}\n\nIngredients:\n{ingredients}\n\nDirections:\n{directions}"
    assert recipe_text(name, ingredients, directions, facts=(("Servings", servings),)) == historic


def test_a_free_text_source_wins_over_the_url():
    assert source_citation("Grandma", "https://example.com/pie") == classify_source("Grandma").citation


def test_a_web_url_is_cited_as_the_web_page():
    assert source_citation(None, "https://example.com/pie") == web_citation("https://example.com/pie")


def test_a_non_http_url_and_no_source_is_the_uncited_citation():
    assert source_citation(None, "mela://recipe/123") == classify_source(None).citation
    assert source_citation(None, None) == classify_source(None).citation
