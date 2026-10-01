"""PdfReader on a short PDF: one chunk for the whole document, and an untitled PDF names no book.

A recipe that runs from one page to the next — a two-page scan of a cookbook — was cut in two
by the reader's one-chunk-per-page split, so the model saw half a recipe each time. A document
whose text fits in one chunk is now sent whole. And a PDF with no title metadata is not a book
the reader knows: the model's reading of the page names the source, as it does for a photo.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import fitz  # PyMuPDF

from recipeparser.config import MAX_CHUNK_CHARS
from recipeparser.io.readers.pdf import PdfReader


def _text_pdf(tmp_path: Path, pages: List[str], title: Optional[str] = None, name: str = "doc.pdf") -> str:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(36, 36, 576, 806), text, fontsize=6)
    if title:
        doc.set_metadata({"title": title, "author": "A. Cook"})
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return str(path)


# Each page clears the scan pre-flight (100 characters a page) on its own.
PAGE_ONE = "Beef Stew\nServes 6\n2 lb beef chuck\n3 carrots\n2 onions\n" + "Brown the beef in batches. " * 6
PAGE_TWO = "Add the carrots and onions and simmer for two hours.\n" + "Season and serve with bread. " * 6


def test_a_recipe_across_a_page_break_reaches_the_model_as_one_chunk(tmp_path):
    chunks = PdfReader().read(_text_pdf(tmp_path, [PAGE_ONE, PAGE_TWO]))
    assert len(chunks) == 1
    text = chunks[0].text
    assert "Beef Stew" in text and "simmer for two hours" in text
    assert text.index("Beef Stew") < text.index("simmer for two hours")


def test_a_document_too_long_for_one_chunk_keeps_its_page_chunks(tmp_path):
    # Three pages that together clear the chunk cap: a cookbook, read page by page as before.
    filler = "Cookbook prose about stews and braises. "
    page = (filler * (MAX_CHUNK_CHARS // 2 // len(filler) + 1))[: MAX_CHUNK_CHARS // 2]
    chunks = PdfReader().read(_text_pdf(tmp_path, [page, page, page]))
    assert len(chunks) == 3


def test_an_untitled_pdf_leaves_the_source_to_the_model(tmp_path):
    chunks = PdfReader().read(_text_pdf(tmp_path, [PAGE_ONE, PAGE_TWO]))
    assert chunks[0].citation is None


def test_a_titled_pdf_is_still_the_book_its_metadata_names(tmp_path):
    chunks = PdfReader().read(_text_pdf(tmp_path, [PAGE_ONE, PAGE_TWO], title="Sunday Suppers"))
    citation = chunks[0].citation
    assert citation is not None
    assert (citation.kind, citation.title, citation.author) == ("book", "Sunday Suppers", "A. Cook")


def test_a_long_untitled_pdf_stays_one_unknown_book(tmp_path):
    # Read page by page, its recipes keep one source rather than each page's model reading.
    filler = "Cookbook prose about stews and braises. "
    page = (filler * (MAX_CHUNK_CHARS // 2 // len(filler) + 1))[: MAX_CHUNK_CHARS // 2]
    chunks = PdfReader().read(_text_pdf(tmp_path, [page, page, page]))
    assert {(c.citation.kind, c.citation.key) for c in chunks if c.citation} == {("unknown", "unknown-book")}
    assert all(c.citation is not None for c in chunks)
