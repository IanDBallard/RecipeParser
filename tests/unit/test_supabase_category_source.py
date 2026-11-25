"""Lists never reach the tagging prompt; restores can still resolve them."""
from __future__ import annotations

import httpx
import pytest

from recipeparser.exceptions import CategoryLoadError
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


# ── A failed fetch fails the import; only missing credentials are empty ──────


def _get_answering(monkeypatch, status=None, body=None, raises=None):
    request = httpx.Request("GET", "http://x/rest/v1/categories")

    def fake_get(url, headers=None, params=None, timeout=None):
        if raises is not None:
            raise raises
        return httpx.Response(status, json=body, request=request)

    monkeypatch.setattr(supabase_source.httpx, "get", fake_get)
    return SupabaseCategorySource(supabase_url="http://x", service_key="k")


@pytest.mark.parametrize("method", ["load_axes", "load_category_ids", "load_parents"])
def test_an_http_error_raises_rather_than_returning_no_categories(monkeypatch, method):
    source = _get_answering(monkeypatch, status=500, body={"message": "boom"})
    with pytest.raises(CategoryLoadError):
        getattr(source, method)("u1")


def test_a_network_error_raises(monkeypatch):
    source = _get_answering(monkeypatch, raises=httpx.ConnectTimeout("timed out"))
    with pytest.raises(CategoryLoadError):
        source.load_axes("u1")


def test_an_unexpected_response_shape_raises(monkeypatch):
    source = _get_answering(monkeypatch, status=200, body={"not": "a list"})
    with pytest.raises(CategoryLoadError):
        source.load_axes("u1")


def test_a_user_with_no_categories_is_still_empty(monkeypatch):
    source = _get_answering(monkeypatch, status=200, body=[])
    assert source.load_axes("u1") == {}
    assert source.load_category_ids("u1") == {}


def test_missing_credentials_are_still_empty(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert SupabaseCategorySource().load_axes("u1") == {}
