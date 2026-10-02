"""
UrlReader's direct fallback: when Jina refuses a page, the reader fetches it itself.

Regression for seriouseats.com, 2026-10-02: Jina answered HTTP 451 for the whole
domain, so every Serious Eats URL failed as "could not be fetched (HTTP 451)."
while the site serves the page, with its schema.org Recipe JSON-LD, to an
ordinary browser GET.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
import requests

from recipeparser.exceptions import SiteRefusedError, UrlFetchError
from recipeparser.io.readers.url import (
    UrlReader,
    _duration,
    _resolves_to_public,
    find_json_ld_recipe,
    recipe_json_ld_to_text,
)

SOURCE = "https://www.seriouseats.com/5-ingredient-black-bean-soup-recipe-11888713"


def _jina_refusal(status: int = 451) -> MagicMock:
    response = MagicMock(status_code=status, text="SecurityCompromiseError: domain blocked")
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    return response


def _page(
    html: str,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    location: Optional[str] = None,
) -> MagicMock:
    headers: Dict[str, str] = {"content-type": content_type}
    if location is not None:
        headers["location"] = location
    return MagicMock(status_code=status, text=html, headers=headers)


def _html_with_json_ld(data: Any, body: str = "<p>Some article text.</p>") -> str:
    return (
        "<html><head><title>Black Bean Soup</title>"
        '<script type="application/ld+json">{ not json }</script>'
        f'<script type="application/ld+json">{json.dumps(data)}</script>'
        f"</head><body>{body}</body></html>"
    )


# Shaped like Dotdash Meredith's markup: a top-level list, a multi-valued @type,
# HowToStep instructions and an HTML entity in a string.
_SERIOUS_EATS_LD: List[Dict[str, Any]] = [
    {
        "@context": "http://schema.org",
        "@type": ["Recipe", "NewsArticle"],
        "headline": "5-Ingredient Black Bean Soup",
        "name": "5-Ingredient Black Bean Soup",
        "description": "Smoky &amp; rich, in under an hour.",
        "recipeYield": ["4", "4 servings"],
        "prepTime": "PT10M",
        "cookTime": "PT40M",
        "totalTime": "PT1H30M",
        "recipeIngredient": [
            "2 tablespoons olive oil",
            "1 large onion, diced",
            "3 (15-ounce) cans black beans",
        ],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": "Heat the oil and cook the onion until soft."},
            {
                "@type": "HowToSection",
                "name": "To finish",
                "itemListElement": [{"@type": "HowToStep", "text": "Add the beans and simmer."}],
            },
        ],
    }
]


@pytest.fixture
def public_dns() -> Any:
    """Every hostname resolves to a public address, so no test does a real DNS lookup."""
    with patch("recipeparser.io.readers.url._resolves_to_public", return_value=True) as mock:
        yield mock


def test_a_jina_refusal_falls_back_to_the_pages_own_recipe_json_ld(public_dns: Any) -> None:
    responses = [_jina_refusal(451), _page(_html_with_json_ld(_SERIOUS_EATS_LD))]
    with patch("recipeparser.io.readers.url.requests.get", side_effect=responses) as mock_get:
        [chunk] = UrlReader().read(SOURCE)

    assert chunk.source_url == SOURCE
    assert chunk.text == (
        "# 5-Ingredient Black Bean Soup\n\n"
        "Smoky & rich, in under an hour.\n\n"
        "Yield: 4, 4 servings\n"
        "Prep time: 10 min\n"
        "Cook time: 40 min\n"
        "Total time: 1 hr 30 min\n\n"
        "Ingredients\n\n"
        "- 2 tablespoons olive oil\n"
        "- 1 large onion, diced\n"
        "- 3 (15-ounce) cans black beans\n\n"
        "Directions\n\n"
        "1. Heat the oil and cook the onion until soft.\n"
        "To finish:\n"
        "2. Add the beans and simmer."
    )
    direct_call = mock_get.call_args_list[1]
    assert direct_call.args == (SOURCE,)
    assert "Chrome" in direct_call.kwargs["headers"]["User-Agent"]
    assert direct_call.kwargs["allow_redirects"] is False


def test_a_page_without_json_ld_falls_back_to_its_article_text(public_dns: Any) -> None:
    html = (
        "<html><head><title>Soup</title><style>p{}</style></head><body>"
        "<nav>Home | Recipes | Shop</nav>"
        "<article><h1>Black Bean Soup</h1><p>2 cans black beans</p>"
        "<script>track()</script></article>"
        "<footer>Copyright</footer></body></html>"
    )
    with patch("recipeparser.io.readers.url.requests.get", side_effect=[_jina_refusal(), _page(html)]):
        [chunk] = UrlReader().read(SOURCE)
    assert chunk.text == "Black Bean Soup\n2 cans black beans"


def test_a_json_ld_recipe_inside_a_graph_is_found() -> None:
    graph = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, {"@type": "Recipe", "name": "Toast"}]}
    recipe = find_json_ld_recipe(_html_with_json_ld(graph))
    assert recipe is not None and recipe["name"] == "Toast"


def test_instructions_given_as_one_string_become_numbered_steps() -> None:
    text = recipe_json_ld_to_text({"name": "Toast", "recipeInstructions": "Toast the bread.\nButter it."})
    assert text == "# Toast\n\nDirections\n\n1. Toast the bread.\n2. Butter it."


def test_redirects_are_followed_by_hand(public_dns: Any) -> None:
    final = _page(_html_with_json_ld(_SERIOUS_EATS_LD))
    hop = _page("", status=301, location="/recipes/black-bean-soup")
    with patch("recipeparser.io.readers.url.requests.get", side_effect=[_jina_refusal(), hop, final]) as mock_get:
        [chunk] = UrlReader().read(SOURCE)
    assert chunk.text.startswith("# 5-Ingredient Black Bean Soup")
    assert mock_get.call_args_list[2].args == ("https://www.seriouseats.com/recipes/black-bean-soup",)


_PASTE = "Open it in your browser, copy the recipe and paste its text in place of the link."


@pytest.mark.parametrize("status", [401, 402, 403, 451])
def test_a_site_that_refuses_the_direct_fetch_is_named_with_what_to_do(public_dns: Any, status: int) -> None:
    """The site's own refusal, not Jina's status: seriouseats.com answered 402 to the server, 2026-10-02."""
    with patch(
        "recipeparser.io.readers.url.requests.get",
        side_effect=[_jina_refusal(451), _page("Payment Required", status=status, content_type="text/plain")],
    ):
        with pytest.raises(SiteRefusedError) as excinfo:
            UrlReader().read(SOURCE)
    assert isinstance(excinfo.value, UrlFetchError)
    assert str(excinfo.value) == f"refuses automated readers (HTTP {status}). {_PASTE}"


def test_any_other_direct_failure_reports_jinas_failure(public_dns: Any) -> None:
    with patch(
        "recipeparser.io.readers.url.requests.get",
        side_effect=[_jina_refusal(451), _page("Not Found", status=404)],
    ):
        with pytest.raises(UrlFetchError) as excinfo:
            UrlReader().read(SOURCE)
    assert not isinstance(excinfo.value, SiteRefusedError)
    assert str(excinfo.value) == "could not be fetched (HTTP 451)."


def test_a_bug_in_the_fallback_still_reports_jinas_failure(public_dns: Any) -> None:
    with patch("recipeparser.io.readers.url.requests.get", side_effect=[_jina_refusal(451), _page("<html>")]), \
         patch("recipeparser.io.readers.url.page_text_from_html", side_effect=RuntimeError("parser bug")):
        with pytest.raises(UrlFetchError) as excinfo:
            UrlReader().read(SOURCE)
    assert str(excinfo.value) == "could not be fetched (HTTP 451)."


def test_a_direct_challenge_page_is_not_read_as_a_recipe(public_dns: Any) -> None:
    challenge = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    with patch("recipeparser.io.readers.url.requests.get", side_effect=[_jina_refusal(), _page(challenge)]):
        with pytest.raises(SiteRefusedError) as excinfo:
            UrlReader().read(SOURCE)
    assert str(excinfo.value) == f"is blocked by the site's bot-protection challenge page. {_PASTE}"


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/latest/meta-data/", "http://localhost:8000/admin", "file:///etc/passwd"],
)
def test_a_private_address_is_never_fetched_directly(url: str) -> None:
    with patch("recipeparser.io.readers.url.requests.get", side_effect=[_jina_refusal()]) as mock_get:
        with pytest.raises(UrlFetchError):
            UrlReader().read(url)
    assert mock_get.call_count == 1  # Jina only


def test_a_redirect_to_a_private_address_is_not_followed() -> None:
    hop = _page("", status=302, location="http://10.0.0.5/secrets")
    with patch("recipeparser.io.readers.url._resolves_to_public", side_effect=lambda host: host != "10.0.0.5"), \
         patch("recipeparser.io.readers.url.requests.get", side_effect=[_jina_refusal(), hop]) as mock_get:
        with pytest.raises(UrlFetchError):
            UrlReader().read(SOURCE)
    assert mock_get.call_count == 2  # Jina, then the first hop; never 10.0.0.5


def test_a_public_name_that_resolves_to_a_private_address_is_not_public() -> None:
    private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    with patch("recipeparser.io.readers.url.socket.getaddrinfo", return_value=private):
        assert _resolves_to_public("evil.example") is False
    with patch("recipeparser.io.readers.url.socket.getaddrinfo", return_value=public + private):
        assert _resolves_to_public("mixed.example") is False
    with patch("recipeparser.io.readers.url.socket.getaddrinfo", return_value=public):
        assert _resolves_to_public("example.com") is True
    with patch("recipeparser.io.readers.url.socket.getaddrinfo", side_effect=socket.gaierror()):
        assert _resolves_to_public("nowhere.invalid") is False


@pytest.mark.parametrize(
    "value, words",
    [("PT10M", "10 min"), ("PT1H30M", "1 hr 30 min"), ("PT2H", "2 hr"), ("P1DT0H", "24 hr"), ("PT0M", ""),
     ("about an hour", "about an hour"), (None, "")],
)
def test_iso_durations_read_as_words(value: Any, words: str) -> None:
    assert _duration(value) == words
