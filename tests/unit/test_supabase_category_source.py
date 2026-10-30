"""Lists never reach the tagging prompt; restores can still resolve them."""
from __future__ import annotations

from recipeparser.io.category_sources import supabase_source
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource
from tests.unit.test_taxonomy import WITH_LISTS


def _source(monkeypatch):
    source = SupabaseCategorySource.__new__(SupabaseCategorySource)
    monkeypatch.setattr(source, "_fetch_categories", lambda user_id: WITH_LISTS)
    return source


def test_load_axes_never_offers_lists(monkeypatch):
    axes = _source(monkeypatch).load_axes("u1")
    assert "Lists" not in axes
    assert "To try" not in [t for tags in axes.values() for t in tags]
    assert set(axes) == {"Course"}


def test_load_parents_has_no_lists(monkeypatch):
    parents = _source(monkeypatch).load_parents("u1")
    assert not ({"To try", "For Sue", "Birthday", "Lists"} & (set(parents) | set(parents.values())))


def test_load_category_ids_still_maps_lists(monkeypatch):
    ids = _source(monkeypatch).load_category_ids("u1")
    assert ids["To try"] == "T" and ids["Birthday"] == "B"


def test_fetch_selects_is_lists_root(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return WITH_LISTS

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.update(params)
        return Resp()

    monkeypatch.setattr(supabase_source.httpx, "get", fake_get)
    source = SupabaseCategorySource(supabase_url="http://x", service_key="k")
    assert source._fetch_categories("u1") == WITH_LISTS
    assert seen["select"] == "id,name,parent_id,is_lists_root"
