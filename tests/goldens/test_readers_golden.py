"""Reader goldens (spec §6.1) — real inputs against checked-in expected output.

No Gemini calls happen here.  Rewrite the expected files with:
    pytest tests/goldens/test_readers_golden.py --update-goldens
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.core.models import Chunk
from recipeparser.exceptions import PdfExtractionError
from recipeparser.io.readers.epub import EpubReader, load_epub
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.readers.pdf import PdfReader, load_pdf
from recipeparser.io.readers.url import UrlReader
from recipeparser.utils import html_to_text
from tests.goldens.paths import READERS_DIR, corpus_path

EPUB_FIXTURES = ("gutenberg-multi.epub", "dual-units.epub", "phases-bakers.epub")


def _chunk_to_dict(chunk: Chunk) -> dict:
    """Everything about a Chunk that JSON can hold and a regression could break."""
    return {
        "input_type": chunk.input_type.value,
        "source_url": chunk.source_url,
        "citation": None if chunk.citation is None else {
            "kind": chunk.citation.kind, "key": chunk.citation.key,
            "title": chunk.citation.title, "author": chunk.citation.author,
        },
        "image_url": chunk.image_url,
        "image_bytes_sha256": (
            hashlib.sha256(chunk.image_bytes).hexdigest() if chunk.image_bytes else None
        ),
        "image_bytes_len": len(chunk.image_bytes) if chunk.image_bytes else 0,
        "image_content_type": chunk.image_content_type,
        "label": chunk.label,
        "pre_parsed_title": getattr(chunk.pre_parsed, "title", None),
        "pre_parsed_embedding_len": (
            None if chunk.pre_parsed_embedding is None else len(chunk.pre_parsed_embedding)
        ),
        "text": chunk.text,
    }


def _assert_golden(name: str, actual: dict, update: bool) -> None:
    path = READERS_DIR / f"{name}.json"
    rendered = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), (
        f"No reader golden at {path}. Create it with:\n"
        "    pytest tests/goldens/test_readers_golden.py --update-goldens"
    )
    assert json.loads(rendered) == json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", EPUB_FIXTURES)
# Narrow, message-matched ignores for ebooklib's own noise only — not
# ``ignore::UserWarning`` wholesale, which would also swallow the
# ``prompt_sha256 mismatch`` UserWarning golden_client.py raises on purpose.
@pytest.mark.filterwarnings(
    "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
)
@pytest.mark.filterwarnings(
    "ignore:This search incorrectly ignores the root element, "
    "and will be fixed in a future version.:FutureWarning"
)
def test_epub_reader_golden(fixture, tmp_path, update_goldens):
    source = str(corpus_path(fixture))
    chunks = EpubReader().read(source)
    _, _, qualifying, _ = load_epub(source, str(tmp_path))
    _assert_golden(
        fixture,
        {
            "chunks": [_chunk_to_dict(c) for c in chunks],
            "qualifying_images": sorted(qualifying),
        },
        update_goldens,
    )


def test_pdf_reader_golden(tmp_path, update_goldens):
    source = str(corpus_path("text-pages.pdf"))
    chunks = PdfReader().read(source)
    _, _, qualifying, _ = load_pdf(source, str(tmp_path))
    _assert_golden(
        "text-pages.pdf",
        {
            "chunks": [_chunk_to_dict(c) for c in chunks],
            "qualifying_images": sorted(qualifying),
        },
        update_goldens,
    )


def test_scanned_pdf_fails_preflight_golden(update_goldens):
    """OCR belongs to the stage layer; the reader's job here is to refuse."""
    with pytest.raises(PdfExtractionError) as excinfo:
        PdfReader().read(str(corpus_path("scanned.pdf")))
    _assert_golden(
        "scanned.pdf",
        {
            "error": "PdfExtractionError",
            "message_prefix": str(excinfo.value)[:48],
        },
        update_goldens,
    )


def test_url_reader_golden(update_goldens):
    """The saved page stands in for what r.jina.ai would return."""
    html = corpus_path("saved-page.html").read_text(encoding="utf-8")
    response = MagicMock()
    response.text = html
    response.raise_for_status = MagicMock()

    with patch("recipeparser.io.readers.url.requests.get", return_value=response) as get:
        chunks = UrlReader().read("https://example.invalid/tomato-soup")

    get.assert_called_once()
    assert get.call_args.args[0] == "https://r.jina.ai/https://example.invalid/tomato-soup"
    _assert_golden(
        "saved-page.html",
        {"chunks": [_chunk_to_dict(c) for c in chunks], "qualifying_images": []},
        update_goldens,
    )


def test_html_to_text_golden(update_goldens):
    """The HTML-to-text path the API adapter uses, against the same saved page."""
    html = corpus_path("saved-page.html").read_text(encoding="utf-8")
    _assert_golden(
        "saved-page.html-to-text",
        {"text": html_to_text(html)},
        update_goldens,
    )


def test_paprika_reader_golden(update_goldens):
    chunks = PaprikaReader().read(str(corpus_path("legacy-photo.paprikarecipes")))
    _assert_golden(
        "legacy-photo.paprikarecipes",
        {"chunks": [_chunk_to_dict(c) for c in chunks], "qualifying_images": []},
        update_goldens,
    )


def test_the_legacy_photo_fixture_really_carries_a_photo():
    """Guards the fixture itself: PR #13 is the reason photo_data is in the corpus."""
    chunk = PaprikaReader().read(str(corpus_path("legacy-photo.paprikarecipes")))[0]
    assert chunk.image_bytes, "legacy-photo fixture lost its photo_data"
    assert chunk.pre_parsed is None, "legacy entry must route to the full pipeline"
