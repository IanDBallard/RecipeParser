"""EPUB reading, image extraction, and text chunking — no AI dependency."""
from __future__ import annotations

import logging
import os
import re
import tempfile
import unicodedata
import zipfile
from typing import List, Optional, Set, Tuple

import ebooklib  # type: ignore[import-untyped]
from bs4 import BeautifulSoup  # type: ignore[import-untyped]
from ebooklib import epub
from lxml import etree  # type: ignore[import-untyped]

from recipeparser.config import MAX_CHUNK_CHARS, MIN_PHOTO_BYTES
from recipeparser.core.citation import Citation, book_citation
from recipeparser.core.models import Chunk, InputType
from recipeparser.io.readers import RecipeReader

log = logging.getLogger(__name__)


class _TolerantEpubReader(epub.EpubReader):  # type: ignore[misc]
    """ebooklib's reader, except an NCX that is not a table of contents leaves the TOC empty.

    ebooklib parses the NCX named by the spine's ``toc`` attribute with a recovering
    parser and dereferences the result. A file holding no element at all (a bare XML
    declaration, plain text, nothing) therefore sinks the whole book with
    ``'NoneType' object has no attribute 'find'`` or an XMLSyntaxError. Its
    ``ignore_ncx`` option is no escape: the NCX is still read whenever the book has
    no EPUB3 nav document, which is every EPUB2. The chapters and metadata of such a
    book are intact, and ``recipeparser.toc`` already falls back to the text when the
    TOC is empty, so the TOC is the only thing worth losing.
    """

    def _parse_ncx(self, data: bytes) -> None:
        try:
            root = epub.parse_string(data).getroot()
        except etree.XMLSyntaxError as exc:
            log.warning("EPUB NCX is not parseable XML (%s); table of contents left empty.", exc)
            return
        if root is None or root.find("{%s}navMap" % epub.NAMESPACES["DAISY"]) is None:
            log.warning("EPUB NCX holds no navMap; table of contents left empty.")
            return
        super()._parse_ncx(data)


def read_epub(epub_path: str, options: Optional[dict] = None) -> epub.EpubBook:
    """``ebooklib.epub.read_epub``, through the reader that tolerates an unreadable NCX.

    Every EPUB the package opens goes through here, so the tolerance cannot drift
    between the chapter reader and the TOC extractor.
    """
    reader = _TolerantEpubReader(epub_path, options)
    book = reader.load()
    reader.process()
    return book


class EpubReader(RecipeReader):
    """
    Reads an EPUB file and returns one Chunk per recipe-candidate chapter.

    Each chunk carries:
    - ``text``: chapter text with [IMAGE: filename] breadcrumb markers
    - ``input_type``: InputType.EPUB
    - ``source_url``: None (a book has no URL)
    - ``citation``: the book's title and author, from EPUB DC metadata

    Images are extracted to a temporary directory managed by this reader.
    The caller is responsible for uploading qualifying images to storage
    before the pipeline's ASSEMBLE stage.
    """

    def read(self, source: str) -> List[Chunk]:
        """
        Parse an EPUB file and return recipe-candidate chapters as Chunks.

        Args:
            source: File-system path to the .epub file.

        Returns:
            List of Chunk objects, one per recipe-candidate chapter.
            Chapters that fail the is_recipe_candidate() heuristic are
            excluded.  If all chapters are filtered out, all chapters are
            returned (fallback to avoid returning an empty list).
        """
        with tempfile.TemporaryDirectory(prefix="cayenne_epub_") as output_dir:
            return self._read_in_dir(source, output_dir)

    def _read_in_dir(self, source: str, output_dir: str) -> List[Chunk]:
        """Internal helper — called with a managed temp directory."""
        citation, _image_dir, _qualifying, raw_chunks = load_epub(source, output_dir)

        # Filter to recipe-candidate chapters
        candidate_chunks = [c for c in raw_chunks if is_recipe_candidate(c)]
        if not candidate_chunks:
            # Fallback: return all chapters rather than an empty list
            candidate_chunks = raw_chunks

        chunks: List[Chunk] = []
        for text in candidate_chunks:
            # Split oversized chapters at paragraph boundaries
            for part in split_large_chunk(text):
                chunks.append(
                    Chunk(
                        text=part,
                        input_type=InputType.EPUB,
                        source_url=None,
                        citation=citation,
                        label=None,
                    )
                )

        return chunks


# Font obfuscation is the one use of the EPUB encryption manifest that leaves the
# chapters readable; anything else in it (Adobe ADEPT's AES, LCP, …) is DRM.
_FONT_OBFUSCATION_ALGORITHMS = frozenset({
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
})
_ENCRYPTION_MANIFEST = "META-INF/encryption.xml"
# Above this share of replacement and control characters the "text" is
# ciphertext or binary decoded by force: measured 0.48–1.0 for such chapters,
# 0.0 for prose.
_MAX_UNREADABLE_RATIO = 0.05


def _drm_algorithms(epub_path: str) -> List[str]:
    """The encryption algorithms the manifest declares that are not font obfuscation."""
    with zipfile.ZipFile(epub_path) as archive:
        if _ENCRYPTION_MANIFEST not in archive.namelist():
            return []
        manifest = archive.read(_ENCRYPTION_MANIFEST).decode("utf-8", "replace")
    declared = set(re.findall(r'Algorithm="([^"]+)"', manifest))
    return sorted(a for a in declared if a not in _FONT_OBFUSCATION_ALGORITHMS)


def _unreadable_ratio(text: str) -> float:
    """The share of characters that are U+FFFD or control characters (tabs and newlines excepted)."""
    if not text:
        return 0.0
    bad = sum(
        1 for ch in text
        if ch == "\ufffd" or (unicodedata.category(ch) == "Cc" and ch not in "\t\n\r")
    )
    return bad / len(text)


def _assert_readable(raw_chunks: List[str]) -> None:
    """Refuse a book whose chapters hold no prose, so ciphertext never reaches the model."""
    from recipeparser.exceptions import EpubExtractionError

    text = "\n".join(raw_chunks)
    if not text.strip() or _unreadable_ratio(text) > _MAX_UNREADABLE_RATIO:
        raise EpubExtractionError("contains no readable text.")


def _open_book(epub_path: str) -> epub.EpubBook:
    """The book, or an EpubExtractionError whose message is a predicate without the path."""
    from recipeparser.exceptions import EpubExtractionError

    try:
        algorithms = _drm_algorithms(epub_path)
    except Exception as e:
        # The library's own text may carry the server path; it goes to the
        # log, and the user sees the predicate.
        log.warning("EPUB could not be opened: %s", e)
        raise EpubExtractionError("could not be opened as an EPUB.") from e
    if algorithms:
        log.warning("EPUB declares %s in its encryption manifest — refusing as DRM.", algorithms)
        raise EpubExtractionError("is DRM-protected: its chapters are encrypted and cannot be read.")
    try:
        return read_epub(epub_path)
    except Exception as e:
        log.warning("EPUB could not be opened: %s", e)
        raise EpubExtractionError("could not be opened as an EPUB.") from e


def load_epub(epub_path: str, output_dir: str) -> Tuple[Citation, str, Set[str], List[str]]:
    """
    Load an EPUB and return the standard book-loader tuple.

    Returns:
        (citation, image_dir, qualifying_images, raw_chunks)
    """
    book = _open_book(epub_path)
    citation = get_book_citation(book)
    image_dir, qualifying_images = extract_all_images(book, output_dir)
    raw_chunks = extract_chapters_with_image_markers(book, qualifying_images)
    _assert_readable(raw_chunks)
    return citation, image_dir, qualifying_images, raw_chunks


def extract_all_images(book: epub.EpubBook, output_dir: str) -> Tuple[str, Set[str]]:
    """
    Write qualifying image items from the EPUB to <output_dir>/images/.
    Images smaller than MIN_PHOTO_BYTES are skipped as decorative separators.
    Returns (image_dir_path, qualifying_filenames_set).
    """
    image_dir = os.path.join(output_dir, "images")
    os.makedirs(image_dir, exist_ok=True)

    saved = skipped = 0
    qualifying: Set[str] = set()
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            content = item.get_content()
            file_name = os.path.basename(item.file_name)
            if len(content) < MIN_PHOTO_BYTES:
                skipped += 1
                log.debug("Skipping small image '%s' (%d bytes).", file_name, len(content))
                continue
            file_path = os.path.join(image_dir, file_name)
            with open(file_path, "wb") as f:
                f.write(content)
            qualifying.add(file_name)
            saved += 1

    log.info("Images: %d saved, %d skipped (< %d bytes).", saved, skipped, MIN_PHOTO_BYTES)
    return image_dir, qualifying


def extract_chapters_with_image_markers(
    book: epub.EpubBook,
    qualifying_images: Optional[Set[str]] = None,
) -> List[str]:
    """
    Return one text string per EPUB document item, with <img> tags replaced
    by [IMAGE: filename] breadcrumb markers so the LLM can associate images
    with recipes without needing vision input.

    If ``qualifying_images`` is provided, only images whose basename is in that
    set get a marker inserted — this prevents the LLM from picking small
    decorative or process-diagram images that were filtered out of the archive.
    """
    chunks = []

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_body_content(), "html.parser")

            for img in soup.find_all("img"):
                src = img.get("src", "")
                if src:
                    filename = os.path.basename(src)
                    if qualifying_images is None or filename in qualifying_images:
                        img.replace_with(f"\n[IMAGE: {filename}]\n")
                    else:
                        img.decompose()

            text = soup.get_text(separator="\n", strip=True)
            if text.strip():
                chunks.append(text)

    return chunks


def split_large_chunk(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """
    Split a text chunk that exceeds max_chars at paragraph boundaries, falling
    back to single-line boundaries for any paragraph that is still too large
    on its own, so that we rarely send an oversized request to the LLM. A
    part with no paragraph or line break left to split on is returned intact.
    """
    if len(text) <= max_chars:
        return [text]

    def pack(units: List[str], sep: str) -> List[str]:
        grouped: List[str] = []
        current: List[str] = []
        current_len = 0
        sep_len = len(sep)

        for unit in units:
            unit_len = len(unit) + sep_len
            if current_len + unit_len > max_chars and current:
                grouped.append(sep.join(current))
                current = [unit]
                current_len = unit_len
            else:
                current.append(unit)
                current_len += unit_len

        if current:
            grouped.append(sep.join(current))

        return grouped

    parts = []
    for para_part in pack(text.split("\n\n"), "\n\n"):
        if len(para_part) > max_chars:
            parts.extend(pack(para_part.split("\n"), "\n"))
        else:
            parts.append(para_part)

    return parts


def is_recipe_candidate(text: str) -> bool:
    """
    Lightweight heuristic to skip obviously non-recipe content (TOC, copyright
    pages, author bios, etc.) before spending an API call.

    Requires both:
      - at least 2 distinct unit/cooking keywords (quantity signals)
      - at least 1 structural keyword (ingredients/directions heading or method verb)
    """
    text_lower = text.lower()

    quantity_keywords = [
        "tbsp", "tablespoon", "tsp", "teaspoon", "cup", "ounce", "oz",
        "gram", "lb", "pound", "ml", "litre", "liter",
    ]
    structure_keywords = [
        "ingredients", "directions", "instructions", "method", "preheat",
        "bake", "simmer", "sauté", "saute", "stir", "whisk", "fold", "roast", "boil",
    ]

    quantity_hits = sum(1 for w in quantity_keywords if w in text_lower)
    structure_hits = sum(1 for w in structure_keywords if w in text_lower)

    return quantity_hits >= 2 and structure_hits >= 1


def get_book_source(book: epub.EpubBook) -> str:
    """
    Extract 'Title — Author' from EPUB DC metadata.
    Falls back to 'EPUB Auto-Import' if metadata is absent.
    """
    def _first(key: str) -> str:
        vals = book.get_metadata("DC", key)
        return str(vals[0][0]).strip() if vals else ""

    title = _first("title")
    author = _first("creator")
    if title and author:
        return f"{title} \u2014 {author}"
    return title or "EPUB Auto-Import"


def get_book_citation(book: epub.EpubBook) -> Citation:
    """The book's DC title and creator as a citation; an unknown book when the title is absent."""
    def _first(key: str) -> str:
        vals = book.get_metadata("DC", key)
        return str(vals[0][0]).strip() if vals else ""

    return book_citation(_first("title"), _first("creator"))


def extract_text_from_epub(epub_path: str) -> str:
    """
    Stateless text extraction from an EPUB.
    - Extracts all document chapters.
    - Filters to recipe-candidate chapters using is_recipe_candidate().
    - Returns a single concatenated string.
    """
    book = _open_book(epub_path)

    chapters = extract_chapters_with_image_markers(book, qualifying_images=None)
    _assert_readable(chapters)

    # Filter to recipe-candidate chapters to reduce token count
    recipe_chapters = [c for c in chapters if is_recipe_candidate(c)]
    if not recipe_chapters:
        # Fall back to all chapters if heuristic filters everything out
        recipe_chapters = chapters

    return "\n\n".join(recipe_chapters)
