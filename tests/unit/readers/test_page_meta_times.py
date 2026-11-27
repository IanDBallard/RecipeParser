"""The times a page's JSON-LD Recipe states: the source's own statement, which the model cannot drop (F-877)."""
from __future__ import annotations

import json

from recipeparser.io.readers.url import page_meta_from_html


def _page(recipe: dict) -> str:
    script = f"<script type=\"application/ld+json\">{json.dumps(recipe)}</script>"
    return f"<html><head>{script}</head><body>...</body></html>"


def test_iso_times_read_as_words():
    recipe = {"@type": "Recipe", "prepTime": "PT5M", "cookTime": "PT30M", "totalTime": "PT1H35M"}
    meta = page_meta_from_html(_page(recipe))
    assert (meta.prep_time, meta.cook_time, meta.total_time) == ("5 min", "30 min", "1 hr 35 min")


def test_a_time_the_page_leaves_out_is_none():
    meta = page_meta_from_html(_page({"@type": "Recipe", "prepTime": "PT5M", "totalTime": "PT35M"}))
    assert (meta.prep_time, meta.cook_time, meta.total_time) == ("5 min", None, "35 min")


def test_a_page_without_a_recipe_states_no_times():
    meta = page_meta_from_html("<html><head><meta name=\"description\" content=\"x\"></head></html>")
    assert (meta.prep_time, meta.cook_time, meta.total_time) == (None, None, None)
