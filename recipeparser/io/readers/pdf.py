"""PDF loading, pre-flight assessment, image extraction, and page-based text chunks."""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, List, Optional, Set, Tuple

import fitz  # type: ignore[import-untyped]  # PyMuPDF

from recipeparser.config import (
    MAX_CHUNK_CHARS,
    MIN_PHOTO_BYTES,
    PDF_OCR_MAX_PAGES,
    PDF_PREFLIGHT_MAX_PAGES,
    PDF_PREFLIGHT_MIN_CHARS_PER_PAGE,
    PDF_PREFLIGHT_MIN_PAGES,
    PDF_PREFLIGHT_SAMPLE_PAGES,
)
from recipeparser.core.citation import Citation, book_citation
from recipeparser.core.models import Chunk, InputType
from recipeparser.exceptions import PdfExtractionError
from recipeparser.io.readers import RecipeReader
from recipeparser.io.readers.book_images import images_named_in, inject_hero_markers
from recipeparser.io.readers.epub import split_large_chunk
from recipeparser.io.readers.photo_check import keep_as_photo

log = logging.getLogger(__name__)


class PdfReader(RecipeReader):
    """
    Reads a PDF file and returns one Chunk per page (or page group), or one
    Chunk for the whole document when its text fits in one.

    Each chunk carries:
    - ``text``: page text with [IMAGE: filename] breadcrumb markers
    - ``input_type``: InputType.PDF
    - ``source_url``: None (a book has no URL)
    - ``citation``: the PDF's title and author metadata, if any; None for an
      untitled PDF short enough to be one chunk (see ``_whole_if_short``)
    - ``images``: the bytes of every photo the text marks, keyed by filename

    Images are extracted to a temporary directory that is gone once read()
    returns, so each chunk carries the bytes of the photos it marks.
    """

    def __init__(self, client: Any = None) -> None:
        # With a client, a scan is transcribed by vision OCR instead of refused
        # (Input media 1). Without one — the CLI's text-only paths — the
        # pre-flight refusal stands.
        self._client = client

    def read(self, source: str) -> List[Chunk]:
        """
        Parse a PDF file and return page chunks. A scan is transcribed by
        vision OCR when a client was given.

        Args:
            source: File-system path to the .pdf file.

        Returns:
            List of Chunk objects, one per non-empty page — or a single chunk
            when the whole document fits in ``MAX_CHUNK_CHARS``.

        Raises:
            PdfExtractionError: pre-flight checks (encrypted, no pages, too
                                many pages), the no-client refusal for a
                                text-poor document, or that document's page
                                count over ``PDF_OCR_MAX_PAGES``.
            RuntimeError: the model returned no text for any page (from
                         ``extract_text_via_vision``; the job fails with that
                         message).
        """
        with tempfile.TemporaryDirectory(prefix="cayenne_pdf_") as output_dir:
            return self._read_in_dir(source, output_dir)

    def _read_in_dir(self, source: str, output_dir: str) -> List[Chunk]:
        """Internal helper — called with a managed temp directory."""
        citation, image_dir, _qualifying, raw_chunks = load_pdf(source, output_dir, client=self._client)

        chunks: List[Chunk] = []
        # A photo-only page's image moves onto the page after it; the bytes are
        # read now because image_dir is deleted when read() returns. The hero
        # pass runs on the pages before a short document is joined: it is a rule
        # about a page and the page after it.
        texts, short = _whole_if_short([t for t in inject_hero_markers(raw_chunks) if t.strip()])
        # An untitled PDF of a page or two is a phone scan, not a book the reader
        # knows: "an unknown book" would settle the source (see resolve_citation)
        # and throw away the cookbook the page names. With no citation the model's
        # reading names it, as it does for a photo. A long untitled PDF stays one
        # unknown book, so its recipes keep one source rather than whatever each
        # page's model reading says.
        chunk_citation: Optional[Citation] = None if short and citation.kind == "unknown" else citation
        for text in texts:
            if text.strip():
                chunks.append(
                    Chunk(
                        text=text,
                        input_type=InputType.PDF,
                        source_url=None,
                        citation=chunk_citation,
                        # load_pdf() drops the page number for empty/skipped pages before
                        # returning raw_chunks, so no true page range is in scope here;
                        # a null label is honest, an invented one is not.
                        label=None,
                        images=images_named_in(text, image_dir),
                    )
                )

        return chunks


def _whole_if_short(pages: List[str]) -> Tuple[List[str], bool]:
    """The pages as one chunk when their joined text fits in ``MAX_CHUNK_CHARS``, else unchanged; and whether it fit.

    A recipe that runs onto the next page — a cookbook page photographed or
    scanned as two — was cut in two by the page split, and the model saw half a
    recipe each time. A document that short is a recipe or a few, not a book, and
    a chunk holding several recipes is the shape an EPUB chapter and a scan
    transcript already take. A longer document keeps its page chunks.
    """
    joined = "\n\n".join(pages)
    if len(joined) > MAX_CHUNK_CHARS:
        return pages, False
    return ([joined] if len(pages) > 1 else pages), True


def load_pdf(path: str, output_dir: str, client: Any = None) -> Tuple[Citation, str, Set[str], List[str]]:
    """
    Load a PDF and return the standard book-loader tuple.

    Runs pre-flight (page count, password, page cap), then either extracts
    images and page-based text chunks with [IMAGE: filename] markers, or — for
    a document with little or no text layer — transcribes every page through
    Gemini Vision when ``client`` is given, up to ``PDF_OCR_MAX_PAGES`` (a
    scan is one vision call per page, and a longer scan is refused rather than
    billed page by page). A scan read that way yields the transcript split to
    ``MAX_CHUNK_CHARS``, no images: its page images are the scan itself, not
    photographs of dishes. Without a client a scan is refused, as it always
    was.

    Returns:
        (citation, image_dir, qualifying_images, raw_chunks)
    """
    try:
        doc = fitz.open(path)
    except Exception as e:
        # The library's own text can carry the server path (PyMuPDF names the
        # file on Linux); it goes to the log, and the user sees the predicate.
        log.warning("PDF could not be opened: %s", e)
        raise PdfExtractionError("could not be opened as a PDF.") from e

    try:
        _check_document(doc, path)
        citation = _get_book_citation(doc)
        image_dir = os.path.join(output_dir, "images")
        os.makedirs(image_dir, exist_ok=True)

        if _is_scan(doc, client):
            from recipeparser.gemini import extract_text_via_vision  # noqa: PLC0415

            transcript = extract_text_via_vision(doc, client)
            split_chunks = [part for part in split_large_chunk(transcript) if part.strip()]
            return citation, image_dir, set(), split_chunks

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

        return citation, image_dir, qualifying_images, raw_chunks
    finally:
        doc.close()


def _check_document(doc: "fitz.Document", path: str) -> None:
    """Raise PdfExtractionError for a document nothing can read: no pages, a password, too many pages.

    The messages are predicates about the file and never name ``path``, which
    is the server's temp file: the API prefixes the user's own filename.
    """
    if doc.page_count == 0:
        raise PdfExtractionError("has no pages.")
    if doc.is_encrypted:
        raise PdfExtractionError("is password-protected.")
    if doc.page_count < PDF_PREFLIGHT_MIN_PAGES:
        log.warning("PDF has very few pages (%d): %s", doc.page_count, path)
    if PDF_PREFLIGHT_MAX_PAGES is not None and doc.page_count > PDF_PREFLIGHT_MAX_PAGES:
        raise PdfExtractionError(
            f"has too many pages ({doc.page_count}; the limit is {PDF_PREFLIGHT_MAX_PAGES})."
        )


def _text_density(doc: "fitz.Document") -> Tuple[float, int]:
    """Average extractable characters per page over the first sampled pages, and how many were sampled."""
    sample_pages = min(PDF_PREFLIGHT_SAMPLE_PAGES, doc.page_count)
    total_chars = sum(len(doc[i].get_text()) for i in range(sample_pages))
    return (total_chars / sample_pages if sample_pages else 0.0), sample_pages


def _is_scan(doc: "fitz.Document", client: Any) -> bool:
    """True when the document has too little text to read and must be transcribed.

    The one scanned-PDF test, for the job reader and the CLI alike (F-069). Raises
    PdfExtractionError when a scan cannot be transcribed: there is no client, or it
    has more pages than a transcription takes.
    """
    avg_chars, sample_pages = _text_density(doc)
    if avg_chars >= PDF_PREFLIGHT_MIN_CHARS_PER_PAGE:
        return False
    if client is None:
        raise PdfExtractionError(
            f"has little or no extractable text (avg {avg_chars:.0f} chars/page "
            f"over the first {sample_pages} pages); it may be a scan without OCR."
        )
    if doc.page_count > PDF_OCR_MAX_PAGES:
        raise PdfExtractionError(
            f"has little or no extractable text and {doc.page_count} pages; "
            f"a scan is transcribed page by page, up to {PDF_OCR_MAX_PAGES}."
        )
    log.info("Scanned PDF detected (avg %.0f chars/page) — transcribing through Gemini Vision.", avg_chars)
    return True


def _get_book_citation(doc: "fitz.Document") -> Citation:
    """PDF title and author metadata as a citation; an unknown book when the title is absent."""
    meta = doc.metadata or {}
    return book_citation(meta.get("title"), meta.get("author"))


def _extract_page_images(
    doc: "fitz.Document",
    page: "fitz.Page",
    page_num: int,
    image_dir: str,
) -> List[str]:
    """Extract a page's images and save those that may be photos; return their filenames.

    An image is kept when it is at least MIN_PHOTO_BYTES and looks like a photograph
    (keep_as_photo, F-203): a blank crop or an ornament is never offered to the model.
    """
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
        if not keep_as_photo(filename, image_bytes):
            continue
        filepath = os.path.join(image_dir, filename)
        with open(filepath, "wb") as f:
            f.write(image_bytes)
        filenames.append(filename)
    return filenames


def extract_text_from_pdf(pdf_path: str, client: Any = None) -> str:
    """
    Stateless text extraction from a PDF.
    - Handles pre-flight (password, etc.)
    - Detects scanned PDFs and falls back to Gemini Vision OCR if client is provided.
    - Returns a single concatenated string of all page text.
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        log.warning("PDF could not be opened: %s", e)
        raise PdfExtractionError("could not be opened as a PDF.") from e

    try:
        if doc.page_count == 0:
            raise PdfExtractionError("has no pages.")
        if doc.is_encrypted:
            raise PdfExtractionError("is password-protected.")

        if _is_scan(doc, client):
            from recipeparser.gemini import extract_text_via_vision  # noqa: PLC0415
            return extract_text_via_vision(doc, client)
        pages_text = [doc[i].get_text() for i in range(doc.page_count)]
        return "\n\n".join(p for p in pages_text if p.strip())
    finally:
        doc.close()
