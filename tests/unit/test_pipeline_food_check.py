"""The pipeline asks the food check before it stores a picture (Cayenne Fix Roadmap F-209).

A book's photo (EPUB, PDF: the duck recipe of 2026-10-25 was a web page saved as a PDF, and its
stored picture was the page's stock photo of a family in an SUV) and a Paprika entry's photo are
checked; a Cayenne restore's picture is the cook's own library and is not. The images are
synthesised, told apart by their width, and the model is a stub keyed on that width.
"""
from __future__ import annotations

import io
from typing import Dict, List, Optional
from unittest.mock import patch

from PIL import Image

from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType
from recipeparser.core.picture_check import FoodVerdict
from recipeparser.core.pipeline import RecipePipeline
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.core.stages.extract import Extraction
from tests.unit.test_pipeline import (
    _PATCH_ASSEMBLE,
    _PATCH_EMBED,
    _PATCH_EXTRACT,
    _PATCH_REFINE,
    FAKE_EMBEDDING,
    _assemble_reflecting_image_url,
    _FakeCategorySource,
    _FakeImageStore,
    _make_ingest_response,
    _make_refinement,
    _named,
)


def _jpeg(width: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, 200), (180, 90, 30)).save(out, "JPEG")
    return out.getvalue()


SUV, DISH, LOGO, SALAD = _jpeg(300), _jpeg(310), _jpeg(320), _jpeg(330)
_KINDS = {300: "vehicle", 310: "dish", 320: "logo", 330: "dish"}


class _Ask:
    """A stub model keyed on the picture's width; records each width it was shown."""

    def __init__(self, kinds: Optional[Dict[int, str]] = None, error: Optional[Exception] = None) -> None:
        self._kinds = kinds or _KINDS
        self._error = error
        self.widths: List[int] = []

    def __call__(self, data: bytes, mime_type: str) -> FoodVerdict:
        with Image.open(io.BytesIO(data)) as img:
            self.widths.append(img.width)
        if self._error is not None:
            raise self._error
        return FoodVerdict(kind=self._kinds[img.width])  # type: ignore[arg-type]


def _pipeline(store: Optional[_FakeImageStore], ask: Optional[_Ask]) -> RecipePipeline:
    GlobalRateLimiter().reset()
    return RecipePipeline(
        client=None,
        controller=PipelineController(),
        category_source=_FakeCategorySource(),
        rpm=9999,
        image_store=store,
        categorize_fn=lambda rows, axes, client, parents=None, limiter=None: {},
        food_check=ask,
    )


def _run(chunk: Chunk, extractions, store, ask):
    pipeline = _pipeline(store, ask)
    with patch(_PATCH_EXTRACT, return_value=Extraction(extractions, [])), \
         patch(_PATCH_REFINE, side_effect=lambda raw, **_: _make_refinement(raw.name)), \
         patch(_PATCH_EMBED, return_value=FAKE_EMBEDDING):
        return sorted(pipeline.run([chunk]), key=lambda r: r.title), pipeline


def _book(images: Dict[str, bytes], input_type: InputType = InputType.PDF) -> Chunk:
    return Chunk(
        text="\n".join(f"[IMAGE: {n}]" for n in images) + "\nRoast Duck ...",
        input_type=input_type,
        images=dict(images),
    )


# ── a book page (PDF, EPUB) ─────────────────────────────────────────────────


def test_the_duck_page_whose_only_picture_is_an_suv_stores_no_picture():
    store, ask = _FakeImageStore(), _Ask()
    [duck], _ = _run(_book({"suv.jpg": SUV}), [_named("Duck", "suv.jpg")], store, ask)
    assert store.calls == []
    assert duck.image_url is None and duck.photo is None
    assert ask.widths == [300]


def test_a_food_photo_the_model_named_is_stored_as_before():
    store, ask = _FakeImageStore(), _Ask()
    [duck], _ = _run(_book({"dish.jpg": DISH}, InputType.EPUB), [_named("Duck", "dish.jpg")], store, ask)
    assert store.calls == [DISH]
    assert duck.image_url == "https://example.test/stored.jpg" and duck.photo.data == DISH


def test_a_refused_photo_gives_way_to_the_pages_next_picture_that_is_food():
    """One recipe on the page: its other pictures are its candidates, in page order."""
    store, ask = _FakeImageStore(), _Ask()
    chunk = _book({"suv.jpg": SUV, "logo.jpg": LOGO, "dish.jpg": DISH})
    [duck], _ = _run(chunk, [_named("Duck", "suv.jpg")], store, ask)
    assert store.calls == [DISH]
    assert duck.photo.filename == "dish.jpg"
    assert ask.widths == [300, 320, 310]


def test_at_most_three_pictures_are_asked_about():
    store, ask = _FakeImageStore(), _Ask()
    chunk = _book({"suv.jpg": SUV, "logo.jpg": LOGO, "logo2.jpg": LOGO, "dish.jpg": DISH})
    [duck], _ = _run(chunk, [_named("Duck", "suv.jpg")], store, ask)
    assert store.calls == [] and duck.image_url is None
    assert len(ask.widths) == 3


def test_with_several_recipes_on_the_page_a_refused_photo_is_not_replaced_by_a_neighbours():
    store, ask = _FakeImageStore(), _Ask()
    chunk = _book({"suv.jpg": SUV, "salad.jpg": SALAD})
    duck, salad = _run(chunk, [_named("Duck", "suv.jpg"), _named("Salad", "salad.jpg")], store, ask)[0]
    assert duck.image_url is None
    assert salad.image_url == "https://example.test/stored.jpg"
    assert store.calls == [SALAD]


def test_no_photo_named_means_no_check_and_no_picture():
    store, ask = _FakeImageStore(), _Ask()
    [duck], _ = _run(_book({"dish.jpg": DISH}), [_named("Duck", None)], store, ask)
    assert ask.widths == [] and store.calls == [] and duck.image_url is None


def test_a_check_that_cannot_run_stores_the_photo_as_before():
    store, ask = _FakeImageStore(), _Ask(error=TimeoutError("read timed out"))
    [duck], _ = _run(_book({"suv.jpg": SUV}), [_named("Duck", "suv.jpg")], store, ask)
    assert store.calls == [SUV] and duck.image_url == "https://example.test/stored.jpg"


def test_without_a_food_check_nothing_is_asked_and_the_photo_is_stored():
    """The CLI and every caller that passes none: what shipped before."""
    store = _FakeImageStore()
    [duck], _ = _run(_book({"suv.jpg": SUV}), [_named("Duck", "suv.jpg")], store, None)
    assert store.calls == [SUV]


def test_each_check_takes_a_rate_limiter_slot():
    chunk = {"suv.jpg": SUV, "dish.jpg": DISH}
    _, unchecked = _run(_book(chunk), [_named("Duck", "suv.jpg")], _FakeImageStore(), None)
    without = unchecked._limiter.current_window_count  # REFINE's and EMBED's
    _, checked = _run(_book(chunk), [_named("Duck", "suv.jpg")], _FakeImageStore(), _Ask())
    assert checked._limiter.current_window_count == without + 2


# ── a Paprika entry's photo ─────────────────────────────────────────────────


def test_a_paprika_photo_that_is_not_food_is_neither_stored_nor_carried():
    store, ask = _FakeImageStore(), _Ask()
    chunk = Chunk(text="Pie", input_type=InputType.PAPRIKA_LEGACY, image_bytes=LOGO, image_content_type="image/jpeg")
    [pie], _ = _run(chunk, [_named("Pie", None)], store, ask)
    assert store.calls == []
    assert pie.image_url is None and pie.photo is None


def test_a_paprika_photo_of_food_is_stored():
    store, ask = _FakeImageStore(), _Ask()
    chunk = Chunk(text="Pie", input_type=InputType.PAPRIKA_LEGACY, image_bytes=DISH, image_content_type="image/jpeg")
    [pie], _ = _run(chunk, [_named("Pie", None)], store, ask)
    assert store.calls == [DISH] and pie.image_url == "https://example.test/stored.jpg"


# ── a Cayenne restore ───────────────────────────────────────────────────────


def test_a_cayenne_restores_picture_is_the_cooks_own_and_is_not_checked():
    store, ask = _FakeImageStore(), _Ask()
    chunk = Chunk(
        text="",
        input_type=InputType.PAPRIKA_CAYENNE,
        pre_parsed=_make_ingest_response("R"),
        pre_parsed_embedding=FAKE_EMBEDDING,
        image_bytes=LOGO,
    )
    pipeline = _pipeline(store, ask)
    with patch(_PATCH_ASSEMBLE, side_effect=_assemble_reflecting_image_url):
        [r] = pipeline.run([chunk])
    assert ask.widths == [] and store.calls == [LOGO] and r.image_url == "https://example.test/stored.jpg"
