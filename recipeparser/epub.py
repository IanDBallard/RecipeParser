"""EPUB reading, image extraction, and text chunking — no AI dependency."""
import logging
import os
from typing import List

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

from recipeparser.config import MAX_CHUNK_CHARS, MIN_PHOTO_BYTES

log = logging.getLogger(__name__)


def extract_all_images(book: epub.EpubBook, output_dir: str) -> str:
    """
    Write qualifying image items from the EPUB to <output_dir>/images/.
    Images smaller than MIN_PHOTO_BYTES are skipped as decorative separators.
    Returns the image directory path.
    """
    image_dir = os.path.join(output_dir, "images")
    os.makedirs(image_dir, exist_ok=True)

    saved = skipped = 0
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            content = item.get_content()
            if len(content) < MIN_PHOTO_BYTES:
                skipped += 1
                continue
            file_name = os.path.basename(item.file_name)
            file_path = os.path.join(image_dir, file_name)
            with open(file_path, "wb") as f:
                f.write(content)
            saved += 1

    log.info("Images: %d saved, %d skipped (< %d bytes).", saved, skipped, MIN_PHOTO_BYTES)
    return image_dir


def extract_chapters_with_image_markers(book: epub.EpubBook) -> List[str]:
    """
    Return one text string per EPUB document item, with <img> tags replaced
    by [IMAGE: filename] breadcrumb markers so the LLM can associate images
    with recipes without needing vision input.
    """
    chunks = []

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_body_content(), "html.parser")

            for img in soup.find_all("img"):
                src = img.get("src", "")
                if src:
                    filename = os.path.basename(src)
                    img.replace_with(f"\n[IMAGE: {filename}]\n")

            text = soup.get_text(separator="\n", strip=True)
            if text.strip():
                chunks.append(text)

    return chunks


def split_large_chunk(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """
    Split a text chunk that exceeds max_chars at paragraph boundaries so that
    we never send a single oversized request to the LLM.
    """
    if len(text) <= max_chars:
        return [text]

    parts = []
    paragraphs = text.split("\n\n")
    current: List[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # account for the "\n\n" separator
        if current_len + para_len > max_chars and current:
            parts.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len

    if current:
        parts.append("\n\n".join(current))

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
