"""A URL import's picture passes the food check before it is stored (Cayenne Fix Roadmap F-209).

The page offers its candidates in order (og:image, the JSON-LD Recipe's images, the scraper's
markdown images); each is fetched and checked, and the first that is food is stored. Every other
route reaches the check through RecipePipeline, which is handed the same call.
"""
from __future__ import annotations

import asyncio
import io
import os
import time
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from PIL import Image

# As tests/test_api.py: the auth bypass must be set before the app is imported.
os.environ.setdefault("DISABLE_AUTH", "1")
os.environ.setdefault("TEST_USER_ID", "00000000-0000-0000-0000-000000000001")
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")

from recipeparser.adapters import api as api_mod  # noqa: E402
from recipeparser.adapters.api import (  # noqa: E402
    _active_jobs,
    _upload_image_to_storage,
    _url_picture_candidates,
    app,
)
from recipeparser.core.picture_check import FoodVerdict  # noqa: E402
from recipeparser.io.readers.url import PageMeta  # noqa: E402
from tests.test_api import _JINA_GET, _patch_pipeline_and_writer  # noqa: E402


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (400, 300), (190, 100, 40)).save(out, "JPEG")
    return out.getvalue()


def _ask(kind: str):
    seen: List[int] = []

    def ask(data: bytes, mime_type: str) -> FoodVerdict:
        seen.append(len(data))
        return FoodVerdict(kind=kind)  # type: ignore[arg-type]

    ask.seen = seen  # type: ignore[attr-defined]
    return ask


# ── the candidates, in order ──────────────────────────────────────────────────


def test_the_og_image_then_the_json_ld_images_then_the_markdowns_each_once_at_most_three():
    meta = PageMeta("https://x.test/og.jpg", None, ("https://x.test/og.jpg", "https://x.test/ld.jpg"))
    markdown = "![a](https://x.test/md1.jpg) ![b](https://x.test/md2.jpg)"
    assert _url_picture_candidates(meta, markdown) == [
        "https://x.test/og.jpg", "https://x.test/ld.jpg", "https://x.test/md1.jpg",
    ]


def test_a_badge_is_never_a_candidate_wherever_it_comes_from():
    logo = "https://x.test/assets/site-logo.png"
    meta = PageMeta(logo, None, (logo,))
    markdown = f"og:image: {logo}\n![Logo]({logo}) ![Dish](https://x.test/dish.jpg)"
    assert _url_picture_candidates(meta, markdown) == ["https://x.test/dish.jpg"]


def test_a_page_with_no_picture_has_no_candidates():
    assert _url_picture_candidates(PageMeta(None, None), "1 cup flour") == []


# ── one candidate: fetched, checked, stored ─────────────────────────────────


def _store(url: Optional[str] = "https://storage.test/r.jpg") -> Tuple[MagicMock, object]:
    store = MagicMock()
    store.return_value.put.return_value = url
    return store, patch.object(api_mod, "SupabaseImageStore", store)


def test_a_picture_that_is_not_food_is_not_stored():
    store, patched = _store()
    with patched, patch.object(api_mod, "_fetch_picture", new=AsyncMock(return_value=(_jpeg(), "image/jpeg"))):
        url = asyncio.run(_upload_image_to_storage("https://x.test/suv.jpg", "rid", _ask("vehicle")))
    assert url is None
    store.return_value.put.assert_not_called()


def test_a_picture_of_food_is_stored():
    store, patched = _store()
    data = _jpeg()
    with patched, patch.object(api_mod, "_fetch_picture", new=AsyncMock(return_value=(data, "image/jpeg"))):
        url = asyncio.run(_upload_image_to_storage("https://x.test/dish.jpg", "rid", _ask("dish")))
    assert url == "https://storage.test/r.jpg"
    store.return_value.put.assert_called_once_with(data, "rid", "image/jpeg")


def test_a_check_that_cannot_run_stores_the_picture_as_before():
    def broken(data: bytes, mime_type: str) -> FoodVerdict:
        raise TimeoutError("read timed out")

    store, patched = _store()
    with patched, patch.object(api_mod, "_fetch_picture", new=AsyncMock(return_value=(_jpeg(), "image/jpeg"))):
        url = asyncio.run(_upload_image_to_storage("https://x.test/dish.jpg", "rid", broken))
    assert url == "https://storage.test/r.jpg"


def test_a_picture_that_cannot_be_fetched_is_neither_checked_nor_stored():
    store, patched = _store()
    ask = _ask("dish")
    with patched, patch.object(api_mod, "_fetch_picture", new=AsyncMock(return_value=None)):
        assert asyncio.run(_upload_image_to_storage("https://x.test/gone.jpg", "rid", ask)) is None
    assert ask.seen == []  # type: ignore[attr-defined]
    store.return_value.put.assert_not_called()


# ── the URL job ───────────────────────────────────────────────────────────────


class _Resp:
    text = "Title: Duck\n\n![Dish](https://cdn.site.test/uploads/dish.jpg)\n\n1 duck breast"

    def raise_for_status(self) -> None:
        return None


def _drain(tc: TestClient, body: dict) -> None:
    resp = tc.post("/jobs", json=body)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + 5.0
    while job_id in _active_jobs and time.monotonic() < deadline:
        time.sleep(0.05)


def test_a_refused_og_image_gives_way_to_the_pages_photo():
    stack, _client, mock_pipeline_cls = _patch_pipeline_and_writer()
    upload = AsyncMock(side_effect=[None, "https://storage.test/dish.jpg"])
    with stack, \
         patch(_JINA_GET, return_value=_Resp()), \
         patch.object(api_mod, "_fetch_page_meta",
                      new=AsyncMock(return_value=PageMeta("https://cdn.site.test/suv-advert.jpg", None))), \
         patch.object(api_mod, "_upload_image_to_storage", new=upload), \
         TestClient(app, raise_server_exceptions=False) as tc:
        _drain(tc, {"url": "https://www.epicurious.test/recipes/duck"})

    assert [c.args[0] for c in upload.await_args_list] == [
        "https://cdn.site.test/suv-advert.jpg", "https://cdn.site.test/uploads/dish.jpg",
    ]
    assert all(c.args[2] is not None for c in upload.await_args_list)  # each one checked
    chunks = mock_pipeline_cls.return_value.run.call_args.args[0]
    assert chunks[0].image_url == "https://storage.test/dish.jpg"


def test_no_candidate_that_is_food_is_a_recipe_without_a_picture_not_a_failed_job():
    stack, _client, mock_pipeline_cls = _patch_pipeline_and_writer()
    with stack, \
         patch(_JINA_GET, return_value=_Resp()), \
         patch.object(api_mod, "_fetch_page_meta", new=AsyncMock(return_value=PageMeta(None, None))), \
         patch.object(api_mod, "_upload_image_to_storage", new=AsyncMock(return_value=None)), \
         TestClient(app, raise_server_exceptions=False) as tc:
        _drain(tc, {"url": "https://www.epicurious.test/recipes/duck"})

    mock_pipeline_cls.return_value.run.assert_called_once()
    assert mock_pipeline_cls.return_value.run.call_args.args[0][0].image_url is None


def test_the_pipeline_is_handed_the_food_check_for_every_other_route():
    """A file (PDF, EPUB, Paprika) or pasted text: the pipeline asks before it stores."""
    stack, mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
    with stack, patch.object(api_mod._gemini_mod, "ask_is_food", return_value=FoodVerdict(kind="dish")) as ask, \
            TestClient(app, raise_server_exceptions=False) as tc:
        _drain(tc, {"text": "Boil water. Add pasta."})
        food_check = mock_pipeline_cls.call_args.kwargs["food_check"]
        assert food_check(b"jpeg", "image/jpeg").kind == "dish"
    ask.assert_called_once_with(mock_client.return_value, b"jpeg", "image/jpeg")
