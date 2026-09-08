"""The image backfill's decision-making, with no network and no storage in sight."""
from __future__ import annotations

import base64
from typing import Any, Dict

import scripts.backfill_recipe_images as mod
from scripts.backfill_recipe_images import plan_image_updates

_PIXEL = b"\x89PNG\r\n\x1a\n fake pixel bytes"


def _row(title: str = "Chicken Pie", recipe_id: str = "r1", **over: Any) -> Dict[str, Any]:
    """A recipes row as PostgREST returns it: no image unless stated."""
    row: Dict[str, Any] = {"id": recipe_id, "title": title, "image_url": None}
    row.update(over)
    return row


def _entry(name: str = "Chicken Pie", **over: Any) -> Dict[str, Any]:
    """An archive entry carrying an embedded photo unless stated."""
    entry: Dict[str, Any] = {
        "name": name,
        "photo": "chicken.png",
        "photo_data": base64.b64encode(_PIXEL).decode(),
    }
    entry.update(over)
    return entry


class TestPlanImageUpdates:
    def test_plans_an_upload_for_a_row_with_no_image(self):
        plan = plan_image_updates([_entry()], [_row()])

        assert len(plan.updates) == 1
        update = plan.updates[0]
        assert update.recipe_id == "r1"
        assert update.photo_bytes == _PIXEL
        assert update.content_type == "image/png"
        assert update.remote_url is None

    def test_never_replaces_an_image_the_row_already_has(self):
        # The two recipes that do have a picture got it from the ingest path.
        # Re-uploading over them is churn at best and data loss at worst.
        plan = plan_image_updates(
            [_entry()], [_row(image_url="https://stored.test/existing.jpg")]
        )

        assert plan.updates == []
        assert plan.untouched == 1

    def test_falls_back_to_the_entrys_remote_url_when_no_photo_is_embedded(self):
        entries = [_entry(photo=None, photo_data=None, image_url="https://food.test/pie.jpg")]

        plan = plan_image_updates(entries, [_row()])

        assert len(plan.updates) == 1
        assert plan.updates[0].photo_bytes is None
        assert plan.updates[0].remote_url == "https://food.test/pie.jpg"

    def test_prefers_the_embedded_photo_over_a_remote_url(self):
        # The archive's own bytes cannot rot or start refusing hotlinks.
        entries = [_entry(image_url="https://food.test/pie.jpg")]

        plan = plan_image_updates(entries, [_row()])

        assert plan.updates[0].photo_bytes == _PIXEL
        assert plan.updates[0].remote_url is None

    def test_leaves_a_row_alone_when_the_entry_has_no_picture_at_all(self):
        entries = [_entry(photo=None, photo_data=None)]

        plan = plan_image_updates(entries, [_row()])

        assert plan.updates == []
        assert plan.untouched == 1

    def test_skips_an_entry_whose_photo_data_will_not_decode(self):
        # A recipe without a picture, never a failed run -- the same rule the
        # reader applies. With no remote url there is nothing left to try.
        entries = [_entry(photo_data="!!! not base64 !!!")]

        plan = plan_image_updates(entries, [_row()])

        assert plan.updates == []
        assert plan.untouched == 1

    def test_uses_the_remote_url_when_the_embedded_photo_will_not_decode(self):
        entries = [_entry(photo_data="!!! not base64 !!!", image_url="https://food.test/pie.jpg")]

        plan = plan_image_updates(entries, [_row()])

        assert len(plan.updates) == 1
        assert plan.updates[0].remote_url == "https://food.test/pie.jpg"

    def test_skips_a_title_that_two_rows_share(self):
        # Two recipes of one name leave no way to tell whose photo this is.
        plan = plan_image_updates([_entry()], [_row(recipe_id="r1"), _row(recipe_id="r2")])

        assert plan.updates == []
        assert plan.ambiguous == ["Chicken Pie"]

    def test_skips_a_title_that_two_entries_share(self):
        plan = plan_image_updates([_entry(), _entry()], [_row()])

        assert plan.updates == []
        assert plan.ambiguous == ["Chicken Pie"]

    def test_reports_an_entry_with_no_row_rather_than_creating_one(self):
        plan = plan_image_updates([_entry(name="Beef Pie")], [_row(title="Chicken Pie")])

        assert plan.updates == []
        assert plan.unmatched == ["Beef Pie"]

    def test_matches_past_the_punctuation_the_pipeline_rewrote(self):
        # Same key as the metadata backfill: 25 of 82 apparently-absent entries
        # differed from their row only in punctuation.
        plan = plan_image_updates(
            [_entry(name="Chongqing “Small” Noodles")],
            [_row(title='Chongqing "Small" Noodles')],
        )

        assert len(plan.updates) == 1
        assert plan.updates[0].recipe_id == "r1"

    def test_ignores_an_entry_with_no_name(self):
        # An empty key would otherwise match every untitled row at once.
        plan = plan_image_updates([_entry(name="")], [_row(title="")])

        assert plan.updates == []


class _FakeResponse:
    """Just enough httpx.Response for the two calls this script makes."""

    def __init__(self, content: bytes = b"", headers: Dict[str, str] | None = None, error: Exception | None = None):
        self.content = content
        self.headers = headers or {}
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error


class _FakeStore:
    """A SupabaseImageStore that records instead of uploading."""

    def __init__(self, public_url: str | None = "https://bucket.test/r1.png"):
        self._public_url = public_url
        self.calls: list[tuple[bytes, str, str]] = []

    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> str | None:
        self.calls.append((image_bytes, recipe_id, content_type))
        return self._public_url


class TestFetchRemoteImage:
    def test_returns_the_bytes_and_the_content_type(self, monkeypatch):
        monkeypatch.setattr(
            mod.httpx, "get",
            lambda *a, **k: _FakeResponse(_PIXEL, {"content-type": "image/png"}),
        )

        assert mod.fetch_remote_image("https://food.test/pie.png") == (_PIXEL, "image/png")

    def test_drops_the_charset_some_hosts_append(self, monkeypatch):
        monkeypatch.setattr(
            mod.httpx, "get",
            lambda *a, **k: _FakeResponse(_PIXEL, {"content-type": "image/jpeg; charset=binary"}),
        )

        # A content type with the charset still attached misses _EXTENSIONS and
        # would silently store a PNG under a .jpg key.
        assert mod.fetch_remote_image("https://food.test/pie.jpg")[1] == "image/jpeg"

    def test_gives_up_on_an_http_error_rather_than_raising(self, monkeypatch):
        # One dead link must not end a run of 466.
        monkeypatch.setattr(
            mod.httpx, "get",
            lambda *a, **k: _FakeResponse(error=RuntimeError("404 Not Found")),
        )

        assert mod.fetch_remote_image("https://food.test/gone.jpg")[0] is None

    def test_refuses_a_page_that_is_not_an_image(self, monkeypatch):
        # A site that answers a dead image URL with an HTML "not found" page
        # would otherwise store the page as the recipe's photograph.
        monkeypatch.setattr(
            mod.httpx, "get",
            lambda *a, **k: _FakeResponse(b"<html>gone</html>", {"content-type": "text/html"}),
        )

        assert mod.fetch_remote_image("https://food.test/gone.jpg")[0] is None


class TestStoreAndRecord:
    def _patched(self, monkeypatch):
        """Record the PATCHes this script would send."""
        patches: list[tuple[str, dict]] = []

        def fake_patch(url, params=None, headers=None, json=None, timeout=None):
            patches.append((params["id"], json))
            return _FakeResponse()

        monkeypatch.setattr(mod.httpx, "patch", fake_patch)
        return patches

    def test_uploads_the_embedded_photo_and_points_the_row_at_it(self, monkeypatch):
        patches = self._patched(monkeypatch)
        store = _FakeStore("https://bucket.test/r1.png")
        update = mod.ImageUpdate("r1", "Chicken Pie", _PIXEL, "image/png", None)

        assert mod.store_and_record("https://db.test", "key", store, update) is True

        assert store.calls == [(_PIXEL, "r1", "image/png")]
        assert patches == [("eq.r1", {"image_url": "https://bucket.test/r1.png"})]

    def test_downloads_a_remote_picture_before_storing_it(self, monkeypatch):
        patches = self._patched(monkeypatch)
        monkeypatch.setattr(
            mod.httpx, "get",
            lambda *a, **k: _FakeResponse(_PIXEL, {"content-type": "image/png"}),
        )
        store = _FakeStore("https://bucket.test/r1.png")
        update = mod.ImageUpdate("r1", "Chicken Pie", None, "image/jpeg", "https://food.test/pie.png")

        assert mod.store_and_record("https://db.test", "key", store, update) is True

        # Stored under the type the host served, not the placeholder on the update.
        assert store.calls == [(_PIXEL, "r1", "image/png")]
        assert patches == [("eq.r1", {"image_url": "https://bucket.test/r1.png"})]

    def test_leaves_the_row_untouched_when_the_upload_fails(self, monkeypatch):
        # put() returns None rather than raising. Recording a URL we never got
        # would point the row at nothing.
        patches = self._patched(monkeypatch)
        store = _FakeStore(public_url=None)
        update = mod.ImageUpdate("r1", "Chicken Pie", _PIXEL, "image/png", None)

        assert mod.store_and_record("https://db.test", "key", store, update) is False
        assert patches == []

    def test_leaves_the_row_untouched_when_the_download_fails(self, monkeypatch):
        patches = self._patched(monkeypatch)
        monkeypatch.setattr(
            mod.httpx, "get",
            lambda *a, **k: _FakeResponse(error=RuntimeError("404")),
        )
        store = _FakeStore()
        update = mod.ImageUpdate("r1", "Chicken Pie", None, "image/jpeg", "https://food.test/gone.jpg")

        assert mod.store_and_record("https://db.test", "key", store, update) is False
        assert store.calls == []
        assert patches == []
