"""Stage replay goldens (spec §6.2).

Real recorded Gemini replies through the real parse path, offline.  Re-record
with `pytest tests/goldens --record-gemini` and commit the prompt, the
recordings and the resulting snapshot changes together.
"""
from __future__ import annotations

import pytest
from syrupy.assertion import SnapshotAssertion

from recipeparser import gemini
from recipeparser.core.models import InputType
from recipeparser.core.stages.categorize import categorize
from recipeparser.core.stages.extract import extract
from recipeparser.core.stages.refine import refine
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.readers.pdf import PdfReader, extract_text_from_pdf
from tests.goldens.conftest import FIXED_AXES
from tests.goldens.paths import corpus_path

#: Every fixture that reaches the extract/refine/categorize path, and how its
#: chunks are produced.  scanned.pdf is absent: it never gets past preflight,
#: so its Gemini coverage is the vision test below.
STAGE_FIXTURES = (
    "gutenberg-multi.epub",
    "dual-units.epub",
    "phases-bakers.epub",
    "text-pages.pdf",
    "legacy-photo.paprikarecipes",
)


def _chunks_for(fixture: str, monkeypatch):
    if fixture.endswith(".epub"):
        return EpubReader().read(str(corpus_path(fixture)))
    if fixture.endswith(".pdf"):
        return PdfReader().read(str(corpus_path(fixture)))
    if fixture.endswith(".paprikarecipes"):
        return PaprikaReader().read(str(corpus_path(fixture)))
    raise AssertionError(f"no reader for {fixture}")


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    """A recorded parse retry must not cost a real second of wall clock."""
    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)


@pytest.mark.parametrize("fixture", STAGE_FIXTURES)
# Narrow, message-matched ignores for ebooklib's own noise only (same marks
# test_readers_golden.py::test_epub_reader_golden uses) -- not
# ``ignore::UserWarning`` wholesale, which would also swallow the
# ``prompt_sha256 mismatch`` UserWarning golden_client.py raises on purpose.
@pytest.mark.filterwarnings(
    "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
)
@pytest.mark.filterwarnings(
    "ignore:This search incorrectly ignores the root element, "
    "and will be fixed in a future version.:FutureWarning"
)
def test_stage_golden(fixture, golden_client, snapshot: SnapshotAssertion, monkeypatch):
    client = golden_client(fixture)
    chunks = _chunks_for(fixture, monkeypatch)

    rendered = []
    for chunk in chunks:
        text = chunk.text
        if gemini.needs_table_normalisation(text):
            text = gemini.normalise_baker_table(text, client)
        extractions = extract(
            chunk_text=text,
            client=client,
            units="book",
            plain_text_mode=chunk.input_type == InputType.PAPRIKA_LEGACY,
        )
        for raw in extractions:
            refined = refine(
                raw=raw,
                client=client,
                uom_system="US",
                measure_preference="Volume",
                user_axes=FIXED_AXES,
            )
            rendered.append(
                {
                    "raw": raw.model_dump(),
                    "refined": refined.model_dump(),
                    "categories": categorize(recipe=refined, user_axes=FIXED_AXES),
                }
            )

    assert rendered == snapshot


def test_vision_ocr_golden(golden_client, snapshot: SnapshotAssertion):
    """The scanned fixture's only path through Gemini."""
    client = golden_client("scanned.pdf")
    text = extract_text_from_pdf(str(corpus_path("scanned.pdf")), client)
    assert text == snapshot


@pytest.mark.filterwarnings(
    "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
)
@pytest.mark.filterwarnings(
    "ignore:This search incorrectly ignores the root element, "
    "and will be fixed in a future version.:FutureWarning"
)
def test_the_bakers_table_is_normalised_before_extraction(golden_client):
    """phases-bakers is in the corpus for this branch; assert it actually fires."""
    client = golden_client("phases-bakers.epub")
    chunks = EpubReader().read(str(corpus_path("phases-bakers.epub")))
    triggering = [c for c in chunks if gemini.needs_table_normalisation(c.text)]
    assert triggering, "phases-bakers.epub no longer triggers needs_table_normalisation"
    normalised = gemini.normalise_baker_table(triggering[0].text, client)
    assert normalised != triggering[0].text


@pytest.mark.filterwarnings(
    "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
)
@pytest.mark.filterwarnings(
    "ignore:This search incorrectly ignores the root element, "
    "and will be fixed in a future version.:FutureWarning"
)
def test_every_refined_recipe_keeps_its_grid_inside_the_axes(golden_client, monkeypatch):
    """Clean-grid stripping: a tag outside FIXED_AXES must never survive."""
    valid = {axis: set(tags) for axis, tags in FIXED_AXES.items()}
    client = golden_client("dual-units.epub")
    for chunk in EpubReader().read(str(corpus_path("dual-units.epub"))):
        for raw in extract(chunk_text=chunk.text, client=client, units="book"):
            refined = refine(
                raw=raw, client=client, uom_system="US",
                measure_preference="Volume", user_axes=FIXED_AXES,
            )
            for axis, tags in refined.grid_categories.items():
                assert axis in valid
                assert set(tags) <= valid[axis]
