"""Image-recovery goldens — does each recipe come out with the photo its source gave it?

The e2e goldens compare whole recipes, but they run without an ImageStore, so
``image_url`` is null for every fixture and a lost photo is invisible there.
This family runs the same readers and recorded Gemini replies through
``RecipePipeline`` with an in-memory ImageStore and checks, per recipe, which
source image (by its bytes) ended up as ``image_url``.

A case marked ``xfail(strict=True)`` is a known loss: the test states what the
output should be and fails the suite the moment the pipeline starts meeting it,
so the mark has to be removed in the same change as the fix.

Not covered here, and why:
- ``ImageReader`` (a photo of a recipe page) has no corpus fixture, and whether
  that photo should become the recipe's picture is undecided.
- ``scanned.pdf``: its page images *are* the text (vision OCR); see
  ``NOT_APPLICABLE`` below.
- ``text-pages.pdf`` carries one qualifying image, but on a front-matter page
  with no recipe, so it only proves the pipeline invents nothing.  PDF hero
  recovery needs a fixture with a photo beside a recipe, and that needs a
  recorded extract reply (``--record-gemini``, a real key).
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import zipfile
from typing import Dict, List, Optional

import pytest

from recipeparser import gemini
from recipeparser.core.fsm import PipelineController
from recipeparser.core.pipeline import MAX_CONCURRENT_API_CALLS, RecipePipeline
from recipeparser.core.ports import CategorySource, ImageStore
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.readers.pdf import PdfReader
from recipeparser.io.readers.url import looks_like_badge, page_meta_from_html
from recipeparser.io.writers.cayenne_zip import CayenneZipWriter
from tests.goldens.conftest import FIXED_AXES
from tests.goldens.paths import CORPUS_FIXTURES, corpus_path

#: Stands for "the photo_data of the Paprika entry with this recipe's name".
PAPRIKA_PHOTO = "<photo_data>"
#: A key meaning "every recipe this fixture yields".
EVERY_RECIPE = "*"

_EPUB_LOSS = (
    "EPUB hero images are dropped: EpubReader extracts them to a TemporaryDirectory "
    "that is deleted before read() returns, Chunk has no field for them, and the full "
    "pipeline passes chunk.image_url (always None here) to assemble() and never reads "
    "the photo_filename the extract reply names. The legacy monolith did this; "
    "PIPELINE_REFACTOR.md marked it MOVE and it was deleted in 90a4a54."
)

#: fixture -> {recipe title (or EVERY_RECIPE) -> the source image it must carry, or None}.
#: An EPUB image is named by its basename inside the book.
HERO_IMAGES: Dict[str, Dict[str, Optional[str]]] = {
    "dual-units.epub": {
        "Buttermilk Scones": "scones.jpg",
        "Brown Butter Shortbread": "shortbread.jpg",
    },
    "phases-bakers.epub": {"Overnight Country Loaf": None, "Sandwich Loaf": None},
    "text-pages.pdf": {EVERY_RECIPE: None},
    "legacy-photo.paprikarecipes": {"Boiled Custard": PAPRIKA_PHOTO},
}

KNOWN_LOSSES = {"dual-units.epub": _EPUB_LOSS}

#: Corpus fixtures this family deliberately does not run, with the reason.
NOT_APPLICABLE = {
    "scanned.pdf": "image-only scan: its page renders are the text for vision OCR, not dish photos",
    "gutenberg-multi.epub": (
        "no qualifying images (its reader golden pins qualifying_images == []), and a full "
        "pipeline replay of it does not finish; test_e2e_golden.py does not run it either"
    ),
    "saved-page.html": "the URL path picks its image in the API adapter; see test_url_hero_image_golden",
}

SAVED_PAGE_HERO = "https://example.invalid/images/tomato-soup-hero.jpg"
SAVED_PAGE_LOGO = ("https://example.invalid/static/logo.png", "Example Kitchen logo")

# Same narrow ignores test_e2e_golden.py carries, for the same reasons (see there).
_REPLAY_WARNINGS = [
    pytest.mark.filterwarnings(
        "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
    ),
    pytest.mark.filterwarnings(
        "ignore:This search incorrectly ignores the root element, "
        "and will be fixed in a future version.:FutureWarning"
    ),
    pytest.mark.filterwarnings(r"ignore:prompt_sha256 mismatch for .*extract-\d+\.json:UserWarning"),
    pytest.mark.filterwarnings(r"ignore:prompt_sha256 mismatch for .*refine-\d+\.json:UserWarning"),
]


class _FixedAxesSource(CategorySource):
    def load_axes(self, user_id: str = "") -> Dict[str, List[str]]:
        return FIXED_AXES

    def load_category_ids(self, user_id: str = "") -> Dict[str, str]:
        return {}


class _MemoryImageStore(ImageStore):
    """Keeps every stored image in memory, addressed by the digest of its bytes."""

    def __init__(self) -> None:
        self.by_url: Dict[str, bytes] = {}

    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]:
        url = "mem://" + hashlib.sha256(image_bytes).hexdigest()
        self.by_url[url] = image_bytes
        return url


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)


def _read(fixture: str):
    path = str(corpus_path(fixture))
    if fixture.endswith(".epub"):
        return EpubReader().read(path)
    if fixture.endswith(".pdf"):
        return PdfReader().read(path)
    return PaprikaReader().read(path)


def _source_image(fixture: str, title: str, ref: str) -> bytes:
    """The bytes the source file holds for *ref*."""
    with zipfile.ZipFile(corpus_path(fixture)) as zf:
        if ref == PAPRIKA_PHOTO:
            for name in zf.namelist():
                entry = json.loads(gzip.decompress(zf.read(name)))
                if entry.get("name") == title:
                    return base64.b64decode(entry["photo_data"])
            raise AssertionError(f"{fixture} has no entry named {title!r}")
        matches = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == ref]
        assert len(matches) == 1, f"{fixture} holds {len(matches)} files named {ref!r}"
        return zf.read(matches[0])


def _describe(url: Optional[str], store: _MemoryImageStore, sources: Dict[str, bytes]) -> str:
    if url is None:
        return "no image"
    data = store.by_url.get(url)
    if data is None:
        return f"an image the store never held ({url})"
    names = [ref for ref, b in sources.items() if b == data]
    return f"image {names[0]!r}" if names else "an image that is not in the source"


def _run(fixture: str, golden_client, store: ImageStore):
    GlobalRateLimiter().reset()
    pipeline = RecipePipeline(
        client=golden_client(fixture),
        controller=PipelineController(),
        category_source=_FixedAxesSource(),
        uom_system="US",
        measure_preference="Volume",
        concurrency=MAX_CONCURRENT_API_CALLS,
        rpm=9999,
        image_store=store,
    )
    return pipeline.run(_read(fixture))


def _params():
    for fixture in HERO_IMAGES:
        marks = list(_REPLAY_WARNINGS)
        if fixture in KNOWN_LOSSES:
            marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_LOSSES[fixture]))
        yield pytest.param(fixture, marks=marks, id=fixture)


@pytest.mark.parametrize("fixture", list(_params()))
def test_each_recipe_carries_its_source_image(fixture, golden_client):
    expected = HERO_IMAGES[fixture]
    store = _MemoryImageStore()
    results = _run(fixture, golden_client, store)
    assert results, f"{fixture} produced no recipes"

    titles = sorted(r.title for r in results)
    if EVERY_RECIPE not in expected:
        assert titles == sorted(expected), f"{fixture} yielded {titles}, expected {sorted(expected)}"

    sources = {
        ref: _source_image(fixture, title, ref)
        for title, ref in expected.items() if ref is not None
    }
    wrong = []
    for recipe in sorted(results, key=lambda r: r.title):
        ref = expected.get(recipe.title, expected.get(EVERY_RECIPE))
        want = None if ref is None else sources[ref]
        got = None if recipe.image_url is None else store.by_url.get(recipe.image_url)
        if got != want or (want is None and recipe.image_url is not None):
            wrong.append(
                f"{recipe.title!r}: expected {'no image' if ref is None else repr(ref)}, "
                f"got {_describe(recipe.image_url, store, sources)}"
            )
    assert not wrong, f"{fixture}: " + "; ".join(wrong)


@pytest.mark.filterwarnings(r"ignore:prompt_sha256 mismatch for .*extract-\d+\.json:UserWarning")
@pytest.mark.filterwarnings(r"ignore:prompt_sha256 mismatch for .*refine-\d+\.json:UserWarning")
def test_a_stored_image_survives_the_cayenne_round_trip(golden_client, tmp_path):
    """Flow B: an image_url written to a Cayenne archive comes back on re-import."""
    store = _MemoryImageStore()
    first = _run("legacy-photo.paprikarecipes", golden_client, store)
    assert [r.image_url is not None for r in first] == [True], "precondition: the photo was stored"

    archive = tmp_path / "roundtrip.paprikarecipes"
    CayenneZipWriter(archive).write(first)
    chunks = PaprikaReader().read(str(archive))
    # The URL rides _cayenne_meta; the pipeline prefers chunk.image_url, then that.
    assert [c.pre_parsed.image_url for c in chunks] == [first[0].image_url]

    GlobalRateLimiter().reset()
    again = RecipePipeline(
        client=golden_client("legacy-photo.paprikarecipes"),
        controller=PipelineController(),
        category_source=_FixedAxesSource(),
        rpm=9999,
        image_store=store,
    ).run(chunks)
    assert [r.image_url for r in again] == [first[0].image_url]


def test_url_hero_image_golden():
    """The page's own og:image is the hero; the site logo in its body is not."""
    html = corpus_path("saved-page.html").read_text(encoding="utf-8")
    meta = page_meta_from_html(html)
    assert meta.image_url == SAVED_PAGE_HERO
    assert not looks_like_badge(meta.image_url)
    # The markdown fallback's badge filter is looks_like_badge; the logo must fail it.
    assert looks_like_badge(*SAVED_PAGE_LOGO)


def test_every_corpus_fixture_has_an_image_expectation():
    """A new fixture must say what image each recipe should carry, or why it can't."""
    assert set(HERO_IMAGES) | set(NOT_APPLICABLE) == set(CORPUS_FIXTURES)
    assert not set(HERO_IMAGES) & set(NOT_APPLICABLE)
    assert set(KNOWN_LOSSES) <= set(HERO_IMAGES)
