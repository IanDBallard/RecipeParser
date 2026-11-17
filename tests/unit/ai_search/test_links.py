import pytest

from recipeparser.adapters.ai_search.links import MAX_LINKS, links_sites, to_links
from recipeparser.adapters.ai_search.look import MAX_TITLE_CHARS, UNSEARCHABLE
from recipeparser.adapters.brave import BraveResult

SITES = ["seriouseats.com"]


@pytest.fixture(autouse=True)
def _clean_unsearchable():
    UNSEARCHABLE.clear()
    yield
    UNSEARCHABLE.clear()


def _r(url, title="Title"):
    return BraveResult(url=url, title=title)


def _kw(**over):
    return {"sites": SITES, "exclude": [], "offered": [], **over}


def test_web_scope_asks_the_whole_refused_set():
    UNSEARCHABLE.add(["example-refused.com"])
    sites = links_sites("web", [])
    assert "example-refused.com" in sites
    assert "seriouseats.com" in sites


def test_sites_scope_asks_only_the_cooks_refused_sites():
    got = links_sites("sites", ["food52.com", "seriouseats.com", "epicurious.com"])
    assert got == ["seriouseats.com", "epicurious.com"]


def test_sites_scope_with_no_refused_sites_is_empty():
    assert links_sites("sites", ["food52.com", "cooking.nytimes.com"]) == []


def test_links_keep_three_in_order():
    results = [_r(f"https://seriouseats.com/{i}") for i in range(5)]
    got = to_links(results, **_kw())
    assert MAX_LINKS == 3
    assert [c.url for c in got] == [f"https://seriouseats.com/{i}" for i in range(3)]


def test_links_drop_http_and_off_site():
    results = [
        _r("http://www.seriouseats.com/a"),
        _r("https://other.com/b"),
        _r("https://www.seriouseats.com/c"),
    ]
    assert [c.url for c in to_links(results, **_kw())] == ["https://www.seriouseats.com/c"]


def test_links_drop_excluded_and_offered():
    results = [
        _r("https://www.seriouseats.com/a/"),
        _r("https://seriouseats.com/b"),
        _r("https://seriouseats.com/c"),
    ]
    got = to_links(
        results,
        **_kw(exclude=["https://seriouseats.com/a"], offered=["https://www.seriouseats.com/b/"]),
    )
    assert [c.url for c in got] == ["https://seriouseats.com/c"]


def test_link_title_is_plain_and_clipped():
    long = "x" * (MAX_TITLE_CHARS + 50)
    results = [
        _r("https://seriouseats.com/a", "<strong>Shakshuka</strong>  Recipe"),
        _r("https://seriouseats.com/b", long),
        _r("https://seriouseats.com/c", " <b></b> "),
    ]
    got = to_links(results, **_kw())
    assert len(got) == 2
    assert got[0].title == "Shakshuka Recipe"
    assert len(got[1].title) == MAX_TITLE_CHARS and got[1].title.endswith("…")


def test_unreadable_address_is_skipped():
    got = to_links([_r("https://[bad/x"), _r("https://seriouseats.com/ok")], **_kw())
    assert [c.url for c in got] == ["https://seriouseats.com/ok"]


def test_links_have_gap_zero_and_empty_note():
    (c,) = to_links([_r("https://seriouseats.com/a")], **_kw())
    assert c.gap == 0 and c.note == ""
