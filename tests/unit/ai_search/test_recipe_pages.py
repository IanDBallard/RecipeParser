"""The look's recipe check (F-866): each candidate page is fetched and kept only when it declares a
schema.org Recipe in JSON-LD, or when it cannot be fetched. The fetcher is always a fake here."""

from __future__ import annotations

import json
import threading
import time

import pytest

from recipeparser.adapters.ai_search.look import RawCandidate
from recipeparser.adapters.ai_search.recipe_pages import PAGE_TIMEOUT_S, TOTAL_TIMEOUT_S, keep_recipe_pages
from recipeparser.exceptions import SiteRefusedError, UrlFetchError

RECIPE_HTML = (
    '<html><head><script type="application/ld+json">'
    + json.dumps({"@context": "https://schema.org", "@type": "Recipe", "name": "Gravy"})
    + "</script></head><body>Gravy</body></html>"
)
ROUNDUP_HTML = (
    '<html><head><script type="application/ld+json">'
    + json.dumps({"@context": "https://schema.org", "@type": "ItemList", "name": "13 Sides"})
    + "</script></head><body>13 Crowd-Pleasing Christmas Dinner Sides</body></html>"
)
NO_LD_HTML = "<html><body>Roasting Guides</body></html>"


def _c(url: str, gap: int = 0) -> RawCandidate:
    return RawCandidate(gap=gap, url=url, title="T", note="")


class FakeFetch:
    """Pages by address: a string is the page's HTML, an exception is raised, ``hold`` blocks."""

    def __init__(self, pages: dict[str, object], hold: threading.Event | None = None) -> None:
        self.pages = pages
        self.hold = hold
        self.calls: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, timeout: float) -> str:
        with self._lock:
            self.calls.append((url, timeout))
        page = self.pages[url]
        if page == "hold":
            assert self.hold is not None
            self.hold.wait(10)  # the test releases it, so no thread outlives the run
            return RECIPE_HTML
        if isinstance(page, BaseException):
            raise page
        assert isinstance(page, str)
        return page


def test_timeouts_are_short():
    assert (PAGE_TIMEOUT_S, TOTAL_TIMEOUT_S) == (3.0, 4.0)


def test_a_page_declaring_a_recipe_is_kept_and_one_declaring_none_is_dropped():
    fetch = FakeFetch(
        {"https://a.com/r": RECIPE_HTML, "https://a.com/list": ROUNDUP_HTML, "https://a.com/g": NO_LD_HTML}
    )
    got = keep_recipe_pages([_c("https://a.com/list"), _c("https://a.com/r"), _c("https://a.com/g")], fetch=fetch)
    assert [c.url for c in got] == ["https://a.com/r"]


def test_order_and_fields_are_kept():
    pages = {f"https://a.com/{i}": RECIPE_HTML for i in range(4)}
    cands = [RawCandidate(gap=i % 2, url=f"https://a.com/{i}", title=f"t{i}", note=f"n{i}") for i in range(4)]
    assert keep_recipe_pages(cands, fetch=FakeFetch(pages)) == cands


@pytest.mark.parametrize(
    "error",
    [
        UrlFetchError("did not respond in time."),
        UrlFetchError("could not be fetched (HTTP 500)."),
        SiteRefusedError("refuses automated readers (HTTP 402).", host="a.com"),
    ],
)
def test_a_page_that_cannot_be_fetched_is_kept(error: Exception):
    fetch = FakeFetch({"https://a.com/x": error, "https://a.com/list": ROUNDUP_HTML})
    got = keep_recipe_pages([_c("https://a.com/x"), _c("https://a.com/list")], fetch=fetch)
    assert [c.url for c in got] == ["https://a.com/x"]


def test_an_unexpected_failure_keeps_the_page_and_logs_its_traceback(caplog: pytest.LogCaptureFixture):
    fetch = FakeFetch({"https://a.com/x": RuntimeError("bug")})
    with caplog.at_level("WARNING"):
        got = keep_recipe_pages([_c("https://a.com/x")], fetch=fetch)
    assert [c.url for c in got] == ["https://a.com/x"]
    assert any(r.exc_info is not None for r in caplog.records)


def test_each_fetch_gets_the_per_page_timeout():
    fetch = FakeFetch({"https://a.com/r": RECIPE_HTML, "https://b.com/r": RECIPE_HTML})
    keep_recipe_pages([_c("https://a.com/r"), _c("https://b.com/r")], fetch=fetch, page_timeout=1.5)
    assert sorted(fetch.calls) == [("https://a.com/r", 1.5), ("https://b.com/r", 1.5)]


def test_pages_are_fetched_in_parallel():
    barrier = threading.Barrier(3, timeout=5)

    def fetch(url: str, timeout: float) -> str:
        barrier.wait()  # only passes when all three fetches are in flight at once
        return RECIPE_HTML

    cands = [_c(f"https://a.com/{i}") for i in range(3)]
    assert keep_recipe_pages(cands, fetch=fetch) == cands


def test_a_page_still_loading_at_the_overall_bound_is_kept_and_the_check_returns():
    hold = threading.Event()
    fetch = FakeFetch({"https://slow.com/r": "hold", "https://a.com/list": ROUNDUP_HTML}, hold=hold)
    try:
        started = time.monotonic()
        got = keep_recipe_pages([_c("https://slow.com/r"), _c("https://a.com/list")], fetch=fetch, total_timeout=0.3)
        elapsed = time.monotonic() - started
    finally:
        hold.set()
    assert [c.url for c in got] == ["https://slow.com/r"]
    assert elapsed < 2.0


def test_nothing_to_check_fetches_nothing():
    fetch = FakeFetch({})
    assert keep_recipe_pages([], fetch=fetch) == []
    assert fetch.calls == []
