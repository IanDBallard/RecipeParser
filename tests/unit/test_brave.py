"""Brave Web Search client: query building, request shape, result parsing, errors."""

from __future__ import annotations

import httpx
import pytest

from recipeparser.adapters.brave import (
    MAX_QUERY_CHARS,
    MAX_QUERY_WORDS,
    BraveError,
    BraveResult,
    BraveSearch,
    fit_sites,
    links_query,
)


def _client(handler) -> BraveSearch:
    return BraveSearch("k", transport=httpx.MockTransport(handler))


def test_links_query_joins_sites_with_or():
    assert (
        links_query("pad thai", ["seriouseats.com", "food52.com"])
        == "pad thai (site:seriouseats.com OR site:food52.com)"
    )


ELEVEN = [
    "bbc.co.uk",
    "bbcgoodfood.com",
    "bonappetit.com",
    "epicurious.com",
    "nytimes.com",
    "seriouseats.com",
    "simplyrecipes.com",
    "theguardian.com",
    "thespruceeats.com",
    "example-one.com",
    "example-two.com",
]


def test_links_query_trims_a_long_question_to_brave_limits():
    group = "(" + " OR ".join(f"site:{s}" for s in ELEVEN) + ")"
    question = " ".join(["vegetarian"] * 100)[:1000]
    assert len(question) == 1000
    q = links_query(question, ELEVEN)
    assert q is not None
    assert len(q) <= MAX_QUERY_CHARS and len(q.split()) <= MAX_QUERY_WORDS
    assert q.endswith(" " + group)
    kept = q[: -len(group) - 1].split()
    assert kept and all(w == "vegetarian" for w in kept)  # whole words, from the front


def test_links_query_word_limit_trims_many_short_words():
    q = links_query(" ".join(f"w{i}" for i in range(80)), ["a.com", "b.com"])
    assert q is not None and len(q.split()) == MAX_QUERY_WORDS
    assert q.startswith("w0 w1 ") and q.endswith(" (site:a.com OR site:b.com)")


def test_links_query_short_question_is_unchanged():
    assert links_query("pad  thai", ["a.com"]) == "pad  thai (site:a.com)"


def test_links_query_operator_group_too_long_is_none():
    sites = [f"site{i}-{'x' * 30}.com" for i in range(12)]
    assert links_query("pie", sites) is None
    assert links_query("pie", [f"s{i}.com" for i in range(26)]) is None  # 26 + 25 ORs = 51 words


def test_search_sends_key_and_query():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/res/v1/web/search"
        assert request.headers["X-Subscription-Token"] == "k"
        assert request.headers["Accept"] == "application/json"
        assert request.url.params["q"] == "x"
        assert request.url.params["count"] == "10"
        return httpx.Response(200, json={"web": {"results": [{"url": "https://a.com/r", "title": "R"}]}})

    assert _client(handler).search("x") == [BraveResult("https://a.com/r", "R")]


def test_search_without_web_is_empty():
    assert _client(lambda r: httpx.Response(200, json={})).search("x") == []


def test_search_skips_results_missing_fields():
    body = {
        "web": {
            "results": [
                {"url": "https://a.com/1"},
                {"title": "No url"},
                {"url": "https://a.com/2", "title": "Two"},
            ]
        }
    }
    assert _client(lambda r: httpx.Response(200, json=body)).search("x") == [BraveResult("https://a.com/2", "Two")]


def test_search_raises_on_429_timeout_and_bad_json():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    for handler in (
        lambda r: httpx.Response(429),
        timeout,
        lambda r: httpx.Response(200, content=b"not json"),
    ):
        with pytest.raises(BraveError):
            _client(handler).search("x")


def test_links_query_exact_boundary_is_unchanged():
    group = "(site:a.com)"
    question = "x" * (MAX_QUERY_CHARS - len(group) - 1)
    q = links_query(question, ["a.com"])
    assert q == f"{question} {group}" and len(q) == MAX_QUERY_CHARS
    assert len(links_query(question + "y", ["a.com"])) <= MAX_QUERY_CHARS  # one over is trimmed


def test_fit_sites_keeps_all_when_they_fit():
    assert fit_sites("pad thai", ELEVEN) == ELEVEN


def test_fit_sites_caps_twenty_sites_to_leave_the_question_room():
    sites = [f"refused-site-{i:02d}.com" for i in range(20)]
    question = " ".join(["vegetarian"] * 30)
    kept = fit_sites(question, sites)
    assert 0 < len(kept) < 20 and kept == sites[: len(kept)]
    q = links_query(question, kept)
    assert q is not None and len(q) <= MAX_QUERY_CHARS and len(q.split()) <= MAX_QUERY_WORDS
    assert len(q) - len("(" + " OR ".join(f"site:{s}" for s in kept) + ")") - 1 >= 150


def test_fit_sites_none_fit():
    assert fit_sites("pie " * 100, ["x" * 300 + ".com"]) == []


def test_search_reads_description_and_defaults_to_empty():
    body = {
        "web": {
            "results": [
                {"url": "https://a.com/1", "title": "One", "description": "About one"},
                {"url": "https://a.com/2", "title": "Two"},
                {"url": "https://a.com/3", "title": "Three", "description": 5},
            ]
        }
    }
    got = _client(lambda r: httpx.Response(200, json=body)).search("x")
    assert [r.description for r in got] == ["About one", "", ""]
