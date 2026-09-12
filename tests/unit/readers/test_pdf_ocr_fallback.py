"""load_pdf on the job path: a scan is read through vision OCR, not refused (Input media 1)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest

from recipeparser.config import MAX_CHUNK_CHARS
from recipeparser.core.models import InputType
from recipeparser.exceptions import PdfExtractionError
from recipeparser.io.readers.pdf import PdfReader, load_pdf


def _pdf(tmp_path: Path, text: str, name: str = "scan.pdf") -> str:
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.set_metadata({"title": "Scanned Book", "author": "A. Cook"})
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return str(path)


def _two_page_pdf(tmp_path: Path, name: str = "scan.pdf") -> str:
    """A two-page (blank) PDF — pages carry no text layer, so pre-flight still sees a scan."""
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    doc.set_metadata({"title": "Scanned Book", "author": "A. Cook"})
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return str(path)


def _client(text: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=text)
    return client


def _client_per_page(texts: list) -> MagicMock:
    """A client whose vision call returns the next text in ``texts`` each time
    it is called — one call per page, as ``extract_text_via_vision`` makes."""
    client = MagicMock()
    client.models.generate_content.side_effect = [MagicMock(text=t) for t in texts]
    return client


def test_a_scan_without_a_client_is_still_refused(tmp_path):
    with pytest.raises(PdfExtractionError, match="little or no extractable text"):
        load_pdf(_pdf(tmp_path, ""), str(tmp_path / "out"))


def test_a_scan_with_a_client_is_transcribed_into_one_chunk_when_short(tmp_path):
    client = _client("Soup\n\n2 onions\n\nSimmer.")
    citation, _image_dir, images, raw_chunks = load_pdf(_pdf(tmp_path, ""), str(tmp_path / "out"), client=client)
    assert raw_chunks == ["Soup\n\n2 onions\n\nSimmer."]     # ruling 3 amended: split to the chunk cap
    assert images == set()                                    # the page images are the scan itself
    assert (citation.kind, citation.title, citation.author) == ("book", "Scanned Book", "A. Cook")
    client.models.generate_content.assert_called_once()


def test_a_long_transcript_is_split_to_the_chunk_cap(tmp_path):
    """The OCR branch used to return one chunk for the whole document, up to
    four times MAX_CHUNK_CHARS — exactly the shape that made EXTRACT return
    truncated JSON. It must come back split to the repo's chunk cap instead
    (ruling 3 amended)."""
    # Each page's transcript is comfortably under the cap on its own, but the
    # two together (joined by extract_text_via_vision's blank-line separator)
    # clear it — the shape that lets pack() group them back apart cleanly.
    # One vision call per page, so the mock client returns one page's text per
    # call rather than the whole multi-page transcript in one go.
    unit = "Soup recipe filler. "
    target_len = int(MAX_CHUNK_CHARS * 0.6)
    page_text = (unit * (target_len // len(unit) + 1))[:target_len]
    page_one = f"PAGE-ONE {page_text}"
    page_two = f"PAGE-TWO {page_text}"
    assert len(page_one) + len(page_two) > MAX_CHUNK_CHARS

    client = _client_per_page([page_one, page_two])
    _citation, _image_dir, _images, raw_chunks = load_pdf(
        _two_page_pdf(tmp_path), str(tmp_path / "out"), client=client
    )

    assert len(raw_chunks) > 1
    assert all(chunk.strip() for chunk in raw_chunks)
    assert all(len(chunk) <= MAX_CHUNK_CHARS for chunk in raw_chunks)
    joined = "".join(raw_chunks)
    assert "PAGE-ONE" in joined
    assert "PAGE-TWO" in joined
    assert client.models.generate_content.call_count == 2   # one vision call per page


def test_a_text_pdf_never_calls_the_client(tmp_path):
    client = _client("must not be read")
    long_text = ("Roast chicken. " * 40).strip()              # well over 100 chars on the page
    _c, _d, _i, raw_chunks = load_pdf(_pdf(tmp_path, long_text), str(tmp_path / "out"), client=client)
    assert raw_chunks and "Roast chicken." in raw_chunks[0]
    client.models.generate_content.assert_not_called()


def test_the_reader_passes_its_client_through(tmp_path):
    client = _client("Soup\n\n2 onions\n\nSimmer.")
    chunks = PdfReader(client=client).read(_pdf(tmp_path, ""))
    assert len(chunks) == 1
    assert chunks[0].input_type is InputType.PDF
    assert chunks[0].text.startswith("Soup")
    assert chunks[0].citation.title == "Scanned Book"


def test_the_reader_without_a_client_keeps_the_refusal(tmp_path):
    with pytest.raises(PdfExtractionError):
        PdfReader().read(_pdf(tmp_path, ""))


def test_a_scan_over_the_ocr_cap_is_refused_before_any_vision_call(tmp_path, monkeypatch):
    from recipeparser.io.readers import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "PDF_OCR_MAX_PAGES", 1)
    client = _client("must not be read")
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    path = tmp_path / "two-blank-pages.pdf"
    doc.save(str(path))
    doc.close()
    with pytest.raises(PdfExtractionError, match="up to 1"):
        load_pdf(str(path), str(tmp_path / "out"), client=client)
    client.models.generate_content.assert_not_called()


def test_a_password_protected_document_is_refused_before_any_ocr(tmp_path):
    # PyMuPDF will not save a zero-page document, so the "no pages" branch has
    # no fixture; the password branch of _check_document proves the order.
    client = _client("x")
    doc = fitz.open()
    doc.new_page()
    path = tmp_path / "locked.pdf"
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    doc.close()
    with pytest.raises(PdfExtractionError, match="password-protected"):
        load_pdf(str(path), str(tmp_path / "out"), client=client)
    client.models.generate_content.assert_not_called()
