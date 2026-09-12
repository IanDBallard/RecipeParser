"""load_pdf on the job path: a scan is read through vision OCR, not refused (Input media 1)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest

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


def _client(text: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=text)
    return client


def test_a_scan_without_a_client_is_still_refused(tmp_path):
    with pytest.raises(PdfExtractionError, match="little or no extractable text"):
        load_pdf(_pdf(tmp_path, ""), str(tmp_path / "out"))


def test_a_scan_with_a_client_is_transcribed_into_one_chunk(tmp_path):
    client = _client("Soup\n\n2 onions\n\nSimmer.")
    citation, _image_dir, images, raw_chunks = load_pdf(_pdf(tmp_path, ""), str(tmp_path / "out"), client=client)
    assert raw_chunks == ["Soup\n\n2 onions\n\nSimmer."]     # ruling 3: one transcript, one chunk
    assert images == set()                                    # the page images are the scan itself
    assert (citation.kind, citation.title, citation.author) == ("book", "Scanned Book", "A. Cook")
    client.models.generate_content.assert_called_once()


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
