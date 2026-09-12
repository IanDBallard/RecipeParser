from scripts.restore_source_urls import plan_restore


def _entry(name, url):
    return {"name": name, "source_url": url}


def _row(i, title, url=None):
    return {"id": f"r{i}", "title": title, "source_url": url}


def test_restores_only_where_the_row_has_no_url_and_the_entry_has_one():
    plan = plan_restore(
        [_entry("Tomato Soup", "https://cooking.nytimes.com/r/1"), _entry("Plain", ""), _entry("Kept", "https://a.example/x")],
        [_row(0, "Tomato Soup"), _row(1, "Plain"), _row(2, "Kept", "https://already.example/y")],
    )
    assert plan.updates == [("r0", "Tomato Soup", "https://cooking.nytimes.com/r/1")]
    assert plan.untouched == 2


def test_matches_on_alphanumerics_only_like_the_metadata_backfill():
    plan = plan_restore(
        [_entry("Brown Butter—Braised Leeks", "https://a.example/l")],
        [_row(0, "brown butter braised leeks")],
    )
    assert [u[0] for u in plan.updates] == ["r0"]


def test_ambiguous_and_unmatched_are_reported_not_written():
    plan = plan_restore(
        [_entry("Twins", "https://a.example/1"), _entry("Twins", "https://a.example/2"), _entry("Lost", "https://a.example/3")],
        [_row(0, "Twins")],
    )
    assert plan.updates == []
    assert plan.ambiguous == ["Twins"]
    assert plan.unmatched == ["Lost"]


def test_a_non_http_entry_value_is_not_a_url():
    plan = plan_restore([_entry("Book", "Italian Food — Elizabeth David")], [_row(0, "Book")])
    assert plan.updates == [] and plan.untouched == 1
