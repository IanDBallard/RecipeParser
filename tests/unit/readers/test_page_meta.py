"""The page's own <meta> tags, and what an image URL says about itself."""
from __future__ import annotations

from recipeparser.io.readers.url import PageMeta, looks_like_badge, page_meta_from_html

NYT = """<html><head>
<meta name="description" content="In this quick and spicy weeknight noodle dish, sizzling \
hot oil is poured over red-pepper flakes." data-next-head=""/>
<meta property="og:image" content="https://static01.nyt.com/images/2019/12/18/dining/as-sesame-noodles/merlin_16576961\
7-facebookJumbo.jpg" data-next-head=""/>
<meta property="og:description" content="Should not win over name=description."/>
<meta name="twitter:image" content="https://static01.nyt.com/other.jpg"/>
</head><body>...</body></html>"""


def test_og_image_and_the_description_are_read():
    meta = page_meta_from_html(NYT)
    assert meta == PageMeta(
        image_url="https://static01.nyt.com/images/2019/12/18/dining/as-sesame-noodles/merlin_165769617-facebookJumbo.jpg",
        description="In this quick and spicy weeknight noodle dish, sizzling hot oil is poured over red-pepper flakes.",
    )


def test_twitter_image_and_og_description_are_the_fallbacks():
    html = (
        '<meta name="twitter:image" content="https://x.test/t.jpg">'
        '<meta property="og:description" content="Blurb &amp; more">'
    )
    assert page_meta_from_html(html) == PageMeta(image_url="https://x.test/t.jpg", description="Blurb & more")


def test_single_quotes_and_attribute_order_do_not_matter():
    html = "<META content='https://x.test/p.png' property='og:image'>"
    assert page_meta_from_html(html).image_url == "https://x.test/p.png"


def test_a_relative_or_empty_image_is_no_image():
    assert page_meta_from_html('<meta property="og:image" content="/assets/hero.jpg">').image_url is None
    assert page_meta_from_html('<meta property="og:image" content="">').image_url is None
    assert page_meta_from_html("<html><body>no head</body></html>") == PageMeta(None, None)


def test_the_first_occurrence_of_a_tag_wins():
    html = '<meta property="og:image" content="https://x.test/1.jpg"><meta property="og:image" content="https://x.test/2.jpg">'
    assert page_meta_from_html(html).image_url == "https://x.test/1.jpg"


class TestLooksLikeBadge:
    def test_the_edamam_badge_through_a_next_image_wrapper(self):
        assert looks_like_badge(
            "https://cooking.nytimes.com/_next/image?url=%2Fassets%2Fedamam-logo.png&w=768&q=75", "Image 1"
        )

    def test_logo_in_the_path(self):
        assert looks_like_badge("https://cdn.site.test/img/site-logo.png")

    def test_logo_in_the_alt_only(self):
        assert looks_like_badge("https://cdn.site.test/img/a1b2c3.png", "Site logo")

    def test_svg_is_furniture(self):
        assert looks_like_badge("https://cdn.site.test/icons/share.svg")

    def test_a_photograph_is_not(self):
        assert not looks_like_badge(
            "https://static01.nyt.com/images/2019/12/18/dining/as-sesame-noodles/merlin_165769617-facebookJumbo.jpg",
            "Spicy sesame noodles",
        )
        assert not looks_like_badge("https://cdn.site.test/uploads/2024/dish.jpg", "")

    def test_badge_words_match_whole_tokens_not_substrings(self):
        assert not looks_like_badge("https://cdn.site.test/recipes/iconic-lasagna.jpg")
        assert not looks_like_badge("https://cdn.site.test/uploads/silicone-mold-cookies.jpg")

    def test_the_wrapper_is_parsed_before_the_whole_url_is_unquoted(self):
        # The inner url= value carries its own encoded ?/&/= — decoding the
        # whole URL before urlparse would split it into bogus query params.
        assert not looks_like_badge(
            "https://x.test/_next/image?url=%2Fuploads%2Fdish.jpg%3Fa%3D1%26b%3D2&w=640"
        )
