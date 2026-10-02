"""The F-203 clean-up's decisions, with no network and no database in sight."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image

from scripts.clear_book_non_photos import is_candidate, plan_clears

PHOTO = (Path(__file__).resolve().parents[2] / "fixtures" / "coffee_cc0.jpg").read_bytes()


def _blank() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(out, "JPEG")
    return out.getvalue()


BLANK = _blank()


def _row(recipe_id: str = "r1", **over: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": recipe_id,
        "title": f"Recipe {recipe_id}",
        "image_url": f"https://store.test/{recipe_id}.jpg",
        "image_source": None,
        "source_kind": "book",
    }
    row.update(over)
    return row


def _fetcher(pictures: Dict[str, Optional[bytes]]):
    def fetch(url: str) -> Optional[bytes]:
        return pictures.get(url)

    return fetch


def test_only_a_book_recipe_with_a_picture_that_was_not_generated_is_looked_at() -> None:
    assert is_candidate(_row())
    assert not is_candidate(_row(image_url=None))
    assert not is_candidate(_row(image_source="generated"))
    assert not is_candidate(_row(source_kind="web"))
    assert not is_candidate(_row(source_kind=None))


def test_clears_a_blank_and_keeps_a_photograph() -> None:
    rows = [_row("blank"), _row("photo")]
    plan = plan_clears(rows, _fetcher({"https://store.test/blank.jpg": BLANK, "https://store.test/photo.jpg": PHOTO}))
    assert [c.recipe_id for c in plan.clears] == ["blank"]
    assert "blank" in plan.clears[0].reason
    assert plan.clears[0].image_url == "https://store.test/blank.jpg"
    assert plan.kept == 1


def test_a_picture_it_cannot_fetch_is_reported_and_left_alone() -> None:
    plan = plan_clears([_row("gone")], _fetcher({}))
    assert plan.clears == []
    assert plan.unreadable == [("Recipe gone", "https://store.test/gone.jpg")]


def test_never_fetches_a_row_it_will_not_judge() -> None:
    fetched = []

    def fetch(url: str) -> Optional[bytes]:
        fetched.append(url)
        return BLANK

    plan_clears([_row("web", source_kind="web"), _row("ai", image_source="generated")], fetch)
    assert fetched == []
