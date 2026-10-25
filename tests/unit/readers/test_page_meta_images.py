"""The pictures a page's JSON-LD Recipe declares: candidates for the food check (Cayenne F-209)."""
from __future__ import annotations

import json

from recipeparser.io.readers.url import PageMeta, page_meta_from_html


def _page(recipe: dict, head: str = "") -> str:
    return (
        f"<html><head>{head}<script type=\"application/ld+json\">{json.dumps(recipe)}</script>"
        "</head><body>...</body></html>"
    )


def test_a_recipe_image_string_is_a_candidate():
    meta = page_meta_from_html(_page({"@type": "Recipe", "image": "https://x.test/dish.jpg"}))
    assert meta.images == ("https://x.test/dish.jpg",)


def test_a_list_of_images_and_image_objects_keep_their_order():
    recipe = {
        "@type": "Recipe",
        "image": [
            {"@type": "ImageObject", "url": "https://x.test/1x1.jpg"},
            "https://x.test/4x3.jpg",
            {"@type": "ImageObject", "contentUrl": "https://x.test/16x9.jpg"},
        ],
    }
    assert page_meta_from_html(_page(recipe)).images == (
        "https://x.test/1x1.jpg", "https://x.test/4x3.jpg", "https://x.test/16x9.jpg",
    )


def test_a_recipe_inside_a_graph_is_found_and_relative_or_repeated_addresses_are_dropped():
    page = {"@graph": [{"@type": "WebPage"}, {"@type": ["Recipe"], "image": {
        "url": "https://x.test/dish.jpg"}}, ]}
    meta = page_meta_from_html(_page(page))
    assert meta.images == ("https://x.test/dish.jpg",)
    recipe = {"@type": "Recipe", "image": ["/img/dish.jpg", "https://x.test/a.jpg", "https://x.test/a.jpg"]}
    assert page_meta_from_html(_page(recipe)).images == ("https://x.test/a.jpg",)


def test_no_json_ld_is_no_candidates_and_the_meta_compares_as_before():
    html = '<meta property="og:image" content="https://x.test/og.jpg">'
    assert page_meta_from_html(html) == PageMeta("https://x.test/og.jpg", None)
    assert page_meta_from_html(html).images == ()


def test_the_og_image_and_the_json_ld_images_are_both_read():
    head = '<meta property="og:image" content="https://x.test/og.jpg">'
    meta = page_meta_from_html(_page({"@type": "Recipe", "image": "https://x.test/dish.jpg"}, head))
    assert (meta.image_url, meta.images) == ("https://x.test/og.jpg", ("https://x.test/dish.jpg",))
