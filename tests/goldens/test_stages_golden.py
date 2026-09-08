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


#: How many entries per fixture the stage snapshot records in full. Every
#: entry is still executed and structurally asserted; this caps only the
#: byte-for-byte detail, so the .ambr stays small enough that a reviewer
#: actually reads its diff.
_SNAPSHOT_DETAIL_CAP = 6


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
# The refine prompt gained a phase-preservation rule (see
# gemini.build_refine_prompt). Only phases-bakers.epub was re-recorded against
# it — that is the fixture the rule exists for, and re-recording the other 517
# refine replies would have cost 517 paid API calls to obtain replies whose
# content the rule does not change. Those recordings therefore no longer match
# their prompt's sha256. Replay is unaffected: recordings key off the prompt
# BODY (the raw recipe), which the rule does not touch. Scoped to refine-NN by
# filename so extract/table/vision drift still warns.
@pytest.mark.filterwarnings(
    r"ignore:prompt_sha256 mismatch for .*refine-\d+\.json:UserWarning"
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

    # Every entry is checked structurally, whatever the fixture's size: a tag
    # outside the axes or a recipe that refined to nothing is a real defect
    # wherever it appears, and these assertions are what keep the entries
    # beyond the detail cap honestly covered rather than merely executed.
    valid_axes = {axis: set(tags) for axis, tags in FIXED_AXES.items()}
    for entry in rendered:
        assert entry["refined"]["structured_ingredients"], (
            f"{entry['raw']['name']!r} refined to no ingredients"
        )
        for axis, tags in entry["refined"]["grid_categories"].items():
            assert axis in valid_axes, f"{entry['raw']['name']!r} invented axis {axis!r}"
            assert set(tags) <= valid_axes[axis], (
                f"{entry['raw']['name']!r} has tags outside {axis!r}: {tags}"
            )

    # The snapshot carries every recipe's name but only the first few in full.
    #
    # gutenberg-multi.epub is a whole cookbook: snapshotting all 503 refined
    # recipes produced a 72,401-line .ambr whose 97% majority was that one
    # fixture. A snapshot nobody can read the diff of guards less than it
    # appears to — any refine change produced thousands of changed lines, so
    # the realistic response was --snapshot-update without inspection.
    #
    # names[] still fails on a recipe appearing, vanishing or being renamed
    # anywhere in the book; the structural assertions above still cover every
    # entry; and detail[] keeps a readable sample of the full shape. All 503
    # recordings are still replayed through the real parse path either way.
    assert {
        "recipe_count": len(rendered),
        "names": [entry["raw"]["name"] for entry in rendered],
        "detail": rendered[:_SNAPSHOT_DETAIL_CAP],
    } == snapshot


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
# The refine prompt gained a phase-preservation rule (see
# gemini.build_refine_prompt). Only phases-bakers.epub was re-recorded against
# it — that is the fixture the rule exists for, and re-recording the other 517
# refine replies would have cost 517 paid API calls to obtain replies whose
# content the rule does not change. Those recordings therefore no longer match
# their prompt's sha256. Replay is unaffected: recordings key off the prompt
# BODY (the raw recipe), which the rule does not touch. Scoped to refine-NN by
# filename so extract/table/vision drift still warns.
@pytest.mark.filterwarnings(
    r"ignore:prompt_sha256 mismatch for .*refine-\d+\.json:UserWarning"
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


@pytest.mark.filterwarnings(
    "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
)
@pytest.mark.filterwarnings(
    "ignore:This search incorrectly ignores the root element, "
    "and will be fixed in a future version.:FutureWarning"
)
def test_refine_keeps_the_phase_headings_extraction_produced(golden_client):
    """A multi-phase recipe must still read as phases after refinement.

    gemini's extract prompt is explicit — "Do NOT flatten, merge, or skip any
    phase" — and the recorded extract reply honours it, emitting bold "Phase 1"
    / "Phase 2" heading entries. build_refine_prompt carried no such rule, so
    the refined output dropped those headings: the ingredients and their order
    survived, but a reader lost the grouping that makes an overnight levain
    legible as two separate sessions.

    phases-bakers.epub exists in the corpus for exactly this branch.
    """
    client = golden_client("phases-bakers.epub")
    chunks = EpubReader().read(str(corpus_path("phases-bakers.epub")))

    phase_markers = []
    for chunk in chunks:
        text = chunk.text
        if gemini.needs_table_normalisation(text):
            text = gemini.normalise_baker_table(text, client)
        for raw in extract(chunk_text=text, client=client, units="book", plain_text_mode=False):
            if not any("phase" in item.lower() for item in raw.ingredients + raw.directions):
                continue  # not the multi-phase recipe
            refined = refine(
                raw=raw,
                client=client,
                uom_system="US",
                measure_preference="Volume",
                user_axes=FIXED_AXES,
            )
            lines = [i.fallback_string for i in refined.structured_ingredients]
            lines += [d.text for d in refined.tokenized_directions]
            phase_markers = [line for line in lines if "phase" in line.lower()]

    assert phase_markers, (
        "the refined recipe kept no phase heading at all — extraction produced "
        "them and refinement dropped them"
    )
