from recipeparser.adapters.ai_search.find import (
    SITES_MAX,
    VIDEO_SOCIAL_HOSTS,
    WEB_MAX,
    pick,
    row_note,
    row_title,
    site_names,
    sites_query,
    strip_amp,
    web_query,
)
from recipeparser.adapters.ai_search.look import MAX_NOTE_CHARS, MAX_TITLE_CHARS, normalise_url
from recipeparser.adapters.brave import BraveResult


def _r(url="https://a.com/r", title="Title", **kw):
    return BraveResult(url=url, title=title, **kw)


def _urls(rows):
    return [row.url for row in rows]


def test_constants():
    assert (SITES_MAX, WEB_MAX) == (3, 5)
    assert VIDEO_SOCIAL_HOSTS == {
        "youtube.com",
        "tiktok.com",
        "instagram.com",
        "pinterest.com",
        "facebook.com",
        "x.com",
        "reddit.com",
    }


def test_web_query_adds_recipe_once():
    assert web_query("pad thai") == "pad thai recipe"
    assert web_query("cookies Recipe") == "cookies Recipe"
    assert web_query("best recipes") == "best recipes"


def test_sites_query_groups_hosts():
    assert (
        sites_query("pad thai", ["seriouseats.com", "food52.com"])
        == "pad thai recipe (site:seriouseats.com OR site:food52.com)"
    )
    assert sites_query("x", []) is None


def test_pick_keeps_order_and_limit():
    results = [_r(f"https://a.com/r{i}") for i in range(7)]
    rows = pick(results, hosts=None, seen=set(), limit=5)
    assert _urls(rows) == [f"https://a.com/r{i}" for i in range(5)]
    assert all(row.gap == 0 for row in rows)


def test_pick_drops_http_offsite_and_video_social():
    results = [
        _r("http://a.com/r"),
        _r("https://www.youtube.com/watch?v=1"),
        _r("https://m.facebook.com/p"),
        _r("https://pin.pinterest.com/x"),
        _r("https://other.com/r"),
        _r("https://www.food52.com/r"),
    ]
    assert _urls(pick(results, hosts={"food52.com"}, seen=set(), limit=5)) == ["https://www.food52.com/r"]
    assert _urls(pick(results[:4], hosts=None, seen=set(), limit=5)) == []


def test_exclude_matches_after_normalising():
    seen = {normalise_url("https://seriouseats.com/x")}
    assert pick([_r("https://www.seriouseats.com/x/")], hosts=None, seen=seen, limit=5) == []


def test_pick_fills_seen_for_the_next_group():
    seen: set[str] = set()
    assert len(pick([_r("https://a.com/r")], hosts=None, seen=seen, limit=5)) == 1
    assert normalise_url("https://a.com/r") in seen
    assert pick([_r("https://a.com/r")], hosts=None, seen=seen, limit=5) == []


def test_title_prefers_recipe_block():
    r = _r(title="Shakshuka With Feta Recipe - NYT Cooking", recipe_title="Shakshuka With Feta")
    assert row_title(r) == "Shakshuka With Feta"


def test_title_suffix_trimmed_only_for_the_site():
    assert row_title(_r(title="Shakshuka - Love and Lemons", profile_name="Love and Lemons")) == "Shakshuka"
    r = _r("https://www.recipetineats.com/pad-thai/", "Pad Thai | RecipeTin Eats", profile_name="RecipeTin Eats")
    assert row_title(r) == "Pad Thai"
    r = _r("https://food.com/r", "Pasta - Quick and Easy", profile_name="Food.com")
    assert row_title(r) == "Pasta - Quick and Easy"
    assert row_title(_r(title="<strong>Pad</strong> Thai &amp; Co")) == "Pad Thai & Co"


def test_title_suffix_equal_to_host_or_publisher():
    assert row_title(_r("https://www.food.com/r", "Pasta – food.com")) == "Pasta"
    assert row_title(_r("https://www.food.com/r", "Pasta - www.food.com")) == "Pasta"
    assert row_title(_r("https://x.com/r", "Pasta | NYT Cooking", recipe_publisher="NYT Cooking")) == "Pasta"


def test_title_suffix_contained_in_site_name_is_trimmed():
    r = _r(title="Chicken korma recipe | Good Food", profile_name="BBC Good Food")
    assert row_title(r) == "Chicken korma recipe"


def test_title_is_clipped():
    assert len(row_title(_r(title="x" * 500))) == MAX_TITLE_CHARS


def test_amp_copy_is_the_same_page():
    both = [_r("https://food52.com/recipes/1-x"), _r("https://food52.com/recipes/1-x/amp")]
    assert _urls(pick(both, hosts=None, seen=set(), limit=5)) == ["https://food52.com/recipes/1-x"]
    assert _urls(pick(both[1:], hosts=None, seen=set(), limit=5)) == ["https://food52.com/recipes/1-x"]


def test_strip_amp():
    assert strip_amp("https://a.com/r/amp") == "https://a.com/r"
    assert strip_amp("https://a.com/r/amp/") == "https://a.com/r"
    assert strip_amp("https://a.com/r/amp?x=1#f") == "https://a.com/r?x=1#f"
    assert strip_amp("https://a.com/r/amp/more") == "https://a.com/r/amp/more"
    assert strip_amp("https://a.com/r/ampersand") == "https://a.com/r/ampersand"
    assert strip_amp("https://amp") == "https://amp"
    assert strip_amp("not a url [") == "not a url ["


def test_recipes_first_orders_sites_stably():
    a, b, c, d = (_r(f"https://a.com/{n}", has_recipe=n in "bd") for n in "abcd")
    assert _urls(pick([a, b, c, d], hosts=None, seen=set(), limit=3, recipes_first=True)) == [
        b.url,
        d.url,
        a.url,
    ]
    assert _urls(pick([a, b, c, d], hosts=None, seen=set(), limit=3)) == [a.url, b.url, c.url]


def test_empty_title_drops_the_row():
    assert pick([_r(title="  <b></b> ")], hosts=None, seen=set(), limit=5) == []


def test_note_prefers_recipe_description_then_description():
    assert row_note(_r(description="plain", recipe_description="block")) == "block"
    assert row_note(_r(description="plain")) == "plain"
    assert row_note(_r()) == ""


def test_note_is_clipped_and_plain():
    note = row_note(_r(description="<b>a</b> &amp; " + "y" * 400))
    assert note.startswith("a & y")
    assert len(note) == MAX_NOTE_CHARS


def test_site_names_from_profile():
    results = [
        _r("https://www.a.com/1", profile_name="First"),
        _r("https://a.com/2", profile_name="Second"),
        _r("https://b.com/1"),
    ]
    assert site_names(results) == {"a.com": "First"}


def test_site_names_are_cleaned_and_blank_names_skipped():
    results = [
        _r("https://a.com/1", profile_name="  <b>  </b> "),
        _r("https://a.com/2", profile_name=" Ben &amp; <i>Jerry</i>'s "),
        _r("https://b.com/1", profile_name="&nbsp;"),
    ]
    assert site_names(results) == {"a.com": "Ben & Jerry's"}
