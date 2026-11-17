"""Brave Web Search client: query building, request shape, result parsing, errors."""

from __future__ import annotations

import httpx
import pytest

from recipeparser.adapters.brave import BraveError, BraveResult, BraveSearch, links_query


def _client(handler) -> BraveSearch:
    return BraveSearch("k", transport=httpx.MockTransport(handler))


def test_links_query_joins_sites_with_or():
    assert (
        links_query("pad thai", ["seriouseats.com", "food52.com"])
        == "pad thai (site:seriouseats.com OR site:food52.com)"
    )


def test_search_sends_key_and_query():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/res/v1/web/search"
        assert request.headers["X-Subscription-Token"] == "k"
        assert request.headers["Accept"] == "application/json"
        assert request.url.params["q"] == "x"
        assert request.url.params["count"] == "10"
        return httpx.Response(
            200, json={"web": {"results": [{"url": "https://a.com/r", "title": "R"}]}}
        )

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
    assert _client(lambda r: httpx.Response(200, json=body)).search("x") == [
        BraveResult("https://a.com/2", "Two")
    ]


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
