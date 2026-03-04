"""PDF loading, pre-flight assessment, image extraction, and page-based text chunks."""
import logging
import os
from typing import List, Set, Tuple

import fitz  # PyMuPDF

from recipeparser.config import (
    MIN_PHOTO_BYTES,
    PDF_PREFLIGHT_MAX_PAGES,
    PDF_PREFLIGHT_MIN_CHARS_PER_PAGE,
    PDF_PREFLIGHT_MIN_PAGES,
    PDF_PREFLIGHT_SAMPLE_PAGES,
)
from recipeparser.exceptions import PdfExtractionError

log = logging.getLogger(__name__)


def load_pdf(path: str, output_dir: str) -> Tuple[str, str, Set[str], List[str]]:
    """
    Load a PDF and return the standard book-loader tuple.

    Runs pre-flight (text layer, page count, password), then extracts images
    and page-based text chunks with [IMAGE: filename] markers.

    Returns:
        (book_source, image_dir, qualifying_images, raw_chunks)
    """
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise PdfExtractionError(f"Failed to open PDF '{path}': {e}") from e

    try:
        _preflight(doc, path)
        book_source = _get_book_source(doc, path)
        image_dir = os.path.join(output_dir, "images")
        os.makedirs(image_dir, exist_ok=True)
        qualifying_images: Set[str] = set()
        page_image_lists: List[List[str]] = []  # per-page list of qualifying image filenames

        for page_num in range(len(doc)):
            page = doc[page_num]
            filenames = _extract_page_images(doc, page, page_num, image_dir)
            qualifying_images.update(filenames)
            page_image_lists.append(filenames)

        raw_chunks = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            markers = "".join(f"\n[IMAGE: {f}]\n" for f in page_image_lists[page_num])
            chunk = (markers + text).strip() if (markers or text.strip()) else ""
            if chunk:
                raw_chunks.append(chunk)

        return book_source, image_dir, qualifying_images, raw_chunks
    finally:
        doc.close()


def _preflight(doc: "fitz.Document", path: str) -> None:
    """Raise PdfExtractionError if the PDF fails pre-flight checks."""
    if doc.page_count == 0:
        raise PdfExtractionError(f"PDF has no pages: '{path}'")
    if doc.is_encrypted:
        raise PdfExtractionError(f"PDF is password-protected: '{path}'")

    # Sample first N pages for text
    sample_pages = min(PDF_PREFLIGHT_SAMPLE_PAGES, doc.page_count)
    total_chars = 0
    for i in range(sample_pages):
        total_chars += len(doc[i].get_text())
    avg_chars = total_chars / sample_pages if sample_pages else 0
    if avg_chars < PDF_PREFLIGHT_MIN_CHARS_PER_PAGE:
        raise PdfExtractionError(
            f"PDF has little or no extractable text (avg {avg_chars:.0f} chars/page over first {sample_pages} pages). "
            f"It may be a scan without OCR: '{path}'"
        )

    if doc.page_count < PDF_PREFLIGHT_MIN_PAGES:
        log.warning("PDF has very few pages (%d): %s", doc.page_count, path)
    if PDF_PREFLIGHT_MAX_PAGES is not None and doc.page_count > PDF_PREFLIGHT_MAX_PAGES:
        raise PdfExtractionError(
            f"PDF has too many pages ({doc.page_count}; max {PDF_PREFLIGHT_MAX_PAGES}): '{path}'"
        )


def _get_book_source(doc: "fitz.Document", path: str) -> str:
    """Extract title and author from PDF metadata; fallback to filename or 'PDF Auto-Import'."""
    meta = doc.metadata
    title = (meta.get("title") or "").strip()
    author = (meta.get("author") or "").strip()
    if title and author:
        return f"{title} \u2014 {author}"
    if title:
        return title
    if author:
        return author
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem if stem else "PDF Auto-Import"


def _extract_page_images(
    doc: "fitz.Document",
    page: "fitz.Page",
    page_num: int,
    image_dir: str,
) -> List[str]:
    """Extract images from a page; save those >= MIN_PHOTO_BYTES. Return list of qualifying filenames."""
    filenames: List[str] = []
    image_list = page.get_images(full=True)
    for img_index, img in enumerate(image_list):
        xref = img[0]
        try:
            base = doc.extract_image(xref)
        except Exception:
            continue
        image_bytes = base.get("image")
        ext = base.get("ext", "png")
        if not image_bytes:
            continue
        if len(image_bytes) < MIN_PHOTO_BYTES:
            continue
        filename = f"page{page_num + 1}_img{img_index + 1}.{ext}"
        filepath = os.path.join(image_dir, filename)
        with open(filepath, "wb") as f:
            f.write(image_bytes)
        filenames.append(filename)
    return filenames
