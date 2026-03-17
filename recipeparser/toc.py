"""
TOC extraction, TOC-driven chunking, and recon.

Extracts table-of-contents from EPUB (nav/NCX) or PDF (outline); falls back to
AI parsing of first pages when programmatic TOC is empty. Uses TOC to segment
full text into one chunk per recipe when possible. Recon compares TOC vs
extracted recipe names to report missed recipes.
"""
import logging
import re
from typing import List, Optional, Tuple

from recipeparser.config import (
    MAX_CHUNK_CHARS,
    MIN_TOC_ENTRIES,
    MIN_TOC_MATCH_RATIO,
    MIN_TOC_RECIPE_RATIO,
    TOC_PDF_FRONT_MATTER_PAGES,
)
from recipeparser.epub import split_large_chunk
from recipeparser.gemini import _call_with_retry
from recipeparser.models import TocEntry, TocList, TocRecipeClassification

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TOC extraction: EPUB
# ---------------------------------------------------------------------------

def _flatten_epub_toc(toc_items: list, result: Optional[List[Tuple[str, Optional[str]]]] = None) -> List[Tuple[str, Optional[str]]]:
    """Recursively flatten ebooklib toc into [(title, href), ...] (all nodes)."""
    if result is None:
        result = []
    for item in toc_items:
        if isinstance(item, tuple):
            section, children = item
            title = getattr(section, "title", None) or getattr(section, "label", None)
            href = getattr(section, "href", None)
            if title:
                result.append((str(title).strip(), str(href).strip() if href else None))
            if children:
                _flatten_epub_toc(children, result)
        elif hasattr(item, "title") and hasattr(item, "href"):
            result.append((str(item.title).strip(), str(item.href).strip() if item.href else None))
    return result


def _flatten_epub_toc_leaves_only(toc_items: list, result: Optional[List[Tuple[str, Optional[str]]]] = None) -> List[Tuple[str, Optional[str]]]:
    """
    Flatten ebooklib toc to leaf entries only [(title, href), ...].
    Use for recon so section/chapter headers (e.g. "Part One", "Contents")
    are not counted — only bottom-level TOC items, which are usually recipes.
    """
    if result is None:
        result = []
    for item in toc_items:
        if isinstance(item, tuple):
            section, children = item
            title = getattr(section, "title", None) or getattr(section, "label", None)
            href = getattr(section, "href", None)
            if children:
                _flatten_epub_toc_leaves_only(children, result)
            else:
                if title:
                    result.append((str(title).strip(), str(href).strip() if href else None))
        elif hasattr(item, "title") and hasattr(item, "href"):
            result.append((str(item.title).strip(), str(item.href).strip() if item.href else None))
    return result


def extract_toc_epub(epub_path: str, raw_chunks: List[str], client) -> List[Tuple[str, Optional[int]]]:
    """
    Extract TOC from EPUB: nav/NCX first, then AI parse of first chunks if needed.

    Returns list of (title, page_or_section) where page is int for PDF-style
    or None for EPUB (we use section/href for matching but normalize to None
    for unified handling). For segment-by-TOC we only need titles.
    """
    from ebooklib import epub

    try:
        book = epub.read_epub(epub_path)
    except Exception as e:
        log.warning("Could not open EPUB for TOC extraction: %s", e)
        fallback = _parse_toc_from_text_fallback(raw_chunks[:2], client)
        if fallback:
            return filter_toc_to_recipe_entries(fallback, client)
        return []

    # ebooklib may return a bare Link (not a list) for single-chapter EPUBs.
    # Normalise to a list so the flatten helpers can always iterate safely.
    toc_raw = book.toc
    if toc_raw and not isinstance(toc_raw, (list, tuple)):
        toc_raw = [toc_raw]

    raw = _flatten_epub_toc_leaves_only(toc_raw) if toc_raw else []
    used_leaves_only = True
    if len(raw) < MIN_TOC_ENTRIES and toc_raw:
        raw = _flatten_epub_toc(toc_raw)
        used_leaves_only = False
    entries = [(t, None) for t, _ in raw if t]
    if len(entries) >= MIN_TOC_ENTRIES:
        if used_leaves_only:
            log.info("EPUB TOC: %d entries from nav/NCX (leaf entries only).", len(entries))
        else:
            log.info("EPUB TOC: %d entries from nav/NCX (all levels).", len(entries))
        filtered = filter_toc_to_recipe_entries(entries, client)
        if len(filtered) != len(entries):
            log.info("EPUB TOC: %d → %d recipe entries (AI filter).", len(entries), len(filtered))
        return filtered

    log.info("EPUB TOC: nav/NCX empty or shallow (%d entries) — using AI on first chunks.", len(entries))
    fallback = _parse_toc_from_text_fallback(raw_chunks[:2], client)
    if fallback:
        filtered = filter_toc_to_recipe_entries(fallback, client)
        if len(filtered) != len(fallback):
            log.info("EPUB TOC: AI fallback %d → %d recipe entries (AI filter).", len(fallback), len(filtered))
        return filtered
    return []


# ---------------------------------------------------------------------------
# TOC extraction: PDF
# ---------------------------------------------------------------------------

def extract_toc_pdf(pdf_path: str, raw_chunks: List[str], client) -> List[Tuple[str, Optional[int]]]:
    """
    Extract TOC from PDF: outline/bookmarks first, then AI parse of first pages.

    Returns list of (title, page) where page is 1-based or None.
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            toc = doc.get_toc()
        finally:
            doc.close()
    except Exception as e:
        log.warning("Could not open PDF for TOC extraction: %s", e)
        return _parse_toc_from_text_fallback(raw_chunks[:TOC_PDF_FRONT_MATTER_PAGES], client)

    # PyMuPDF get_toc() returns [[level, title, page], ...]; page is 1-based.
    raw = [(item[1].strip(), int(item[2]) if len(item) > 2 else None) for item in toc if item[1].strip()]
    if len(raw) >= MIN_TOC_ENTRIES:
        log.info("PDF TOC: %d entries from outline.", len(raw))
        filtered = filter_toc_to_recipe_entries(raw, client)
        if len(filtered) != len(raw):
            log.info("PDF TOC: %d → %d recipe entries (AI filter).", len(raw), len(filtered))
        return filtered

    log.info(
        "PDF TOC: outline empty or shallow (%d entries) — using AI on first %d pages.",
        len(raw), TOC_PDF_FRONT_MATTER_PAGES,
    )
    fallback = _parse_toc_from_text_fallback(raw_chunks[:TOC_PDF_FRONT_MATTER_PAGES], client)
    if fallback:
        filtered = filter_toc_to_recipe_entries(fallback, client)
        if len(filtered) != len(fallback):
            log.info("PDF TOC: AI fallback %d → %d recipe entries (AI filter).", len(fallback), len(filtered))
        return filtered
    return []


def _parse_toc_from_text_fallback(
    chunks: List[str],
    client,
) -> List[Tuple[str, Optional[int]]]:
    """Parse TOC from text via AI when programmatic TOC is empty/shallow."""
    if not chunks:
        return []

    text = "\n\n".join(chunks)
    if len(text) > 20_000:
        text = text[:20_000] + "\n[... truncated ...]"

    prompt = """This text is from the table-of-contents or contents page of a recipe book.
Extract the list of recipe/section titles in order. Include page numbers if they appear.

Output JSON: {"entries": [{"title": "...", "page": <int or null>}, ...]}
Use page: null when no page number is given.
Include only substantive entries (skip "Contents", "Index", etc. if they are standalone headers)."""

    prompt += f"\n\nText:\n{text}"

    try:
        response = _call_with_retry(
            client,
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": TocList,
                "temperature": 0,
            },
        )
        parsed = response.parsed
        if parsed and parsed.entries:
            return [(e.title.strip(), e.page) for e in parsed.entries if e.title.strip()]
    except Exception as e:
        log.warning("AI TOC parsing failed: %s", e)
    return []


# ---------------------------------------------------------------------------
# Recipe-name classification and filter
# ---------------------------------------------------------------------------

def _classify_toc_recipe_indices(
    entries: List[Tuple[str, Optional[int]]],
    client,
) -> Optional[List[int]]:
    """
    Use AI to classify which TOC entries are recipe titles vs section headers.
    Returns 0-based indices of recipe entries, or None on failure.
    """
    if not entries:
        return None
    titles = [e[0] for e in entries]
    prompt = """Given this list of table-of-contents entries from a cookbook, identify which ones are
specific recipe or dish names (e.g. "Chocolate Chip Cookies", "Beef Stew", "Roast Chicken")
vs section/chapter headers (e.g. "Soups", "Desserts", "Introduction", "Breakfast").

Return JSON: {"recipe_indices": [0, 1, 3, 5, ...]} — the 0-based indices of entries that are recipe titles.
Section headers like "Soups", "Desserts" should NOT be included. Only include entries that look like
specific dish/recipe names."""

    prompt += "\n\nEntries (one per line):\n"
    for i, t in enumerate(titles):
        prompt += f"{i}. {t}\n"

    try:
        response = _call_with_retry(
            client,
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": TocRecipeClassification,
                "temperature": 0,
            },
        )
        parsed = response.parsed
        if parsed and parsed.recipe_indices is not None:
            return [i for i in parsed.recipe_indices if 0 <= i < len(entries)]
    except Exception as e:
        log.warning("Recipe-name classification failed: %s", e)
    return None


def filter_toc_to_recipe_entries(
    entries: List[Tuple[str, Optional[int]]],
    client,
) -> List[Tuple[str, Optional[int]]]:
    """
    Filter TOC to entries classified as recipe titles (not section headers).
    Uses one AI call. On failure returns entries unchanged so recon still runs.
    """
    if not entries:
        return []
    indices = _classify_toc_recipe_indices(entries, client)
    if indices is None:
        return entries
    return [entries[i] for i in sorted(indices)]


def check_recipe_name_ratio(
    entries: List[Tuple[str, Optional[int]]],
    client,
) -> float:
    """
    Use AI to classify which TOC entries are recipe titles vs section headers.
    Returns fraction (0.0–1.0) of entries classified as recipe names.
    """
    if not entries:
        return 0.0
    indices = _classify_toc_recipe_indices(entries, client)
    if indices is None:
        return 0.0
    return len(indices) / len(entries)


# ---------------------------------------------------------------------------
# Segment by TOC
# ---------------------------------------------------------------------------

def _normalize_for_match(s: str) -> str:
    """Lowercase, collapse runs of whitespace for fuzzy matching."""
    return re.sub(r"\s+", " ", s.lower().strip())


def segment_by_toc(
    raw_chunks: List[str],
    toc_entries: List[Tuple[str, Optional[int]]],
) -> Tuple[List[str], float]:
    """
    Segment full text by TOC entry boundaries.

    For each TOC title in order, finds its first occurrence in the full text.
    Segment i = text from start of title i to start of title i+1 (or end).
    Splits oversize segments with split_large_chunk.

    Returns (segments, match_ratio) where match_ratio = fraction of TOC titles found.
    If match_ratio < MIN_TOC_MATCH_RATIO, caller should fall back to raw chunks.
    """
    if not toc_entries or not raw_chunks:
        return [], 0.0

    delimiter = "\n\n---CHUNK---\n\n"
    full_text = delimiter.join(raw_chunks)
    lower_text = full_text.lower()

    # Collect start positions for each found title
    starts: List[int] = []
    search_from = 0
    found = 0

    for title, _ in toc_entries:
        norm = _normalize_for_match(title)
        if not norm:
            continue
        pos = lower_text.find(norm, search_from)
        if pos >= 0:
            found += 1
            starts.append(pos)
            search_from = pos + 1  # search after this match for next title

    match_ratio = found / len(toc_entries) if toc_entries else 0.0

    segments: List[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(full_text)
        seg = full_text[start:end].strip()
        if seg:
            segments.extend(split_large_chunk(seg, MAX_CHUNK_CHARS))

    if found < len(toc_entries):
        missing = [
            t for t, _ in toc_entries
            if _normalize_for_match(t) and lower_text.find(_normalize_for_match(t)) < 0
        ]
        if missing:
            log.debug("TOC titles not found in text: %s", missing[:5])

    return segments, match_ratio


# ---------------------------------------------------------------------------
# Recon
# ---------------------------------------------------------------------------

def run_recon(
    toc_entries: List[Tuple[str, Optional[int]]],
    extracted_names: List[str],
) -> Tuple[List[str], List[str], List[str]]:
    """
    Compare TOC titles vs extracted recipe names.

    Returns (matched, missing, extra):
      - matched: in both
      - missing: in TOC but not extracted
      - extra: extracted but not in TOC
    """
    toc_titles = [e[0] for e in toc_entries]
    toc_norm = {_normalize_for_match(t): t for t in toc_titles if _normalize_for_match(t)}
    ext_norm = {_normalize_for_match(n): n for n in extracted_names if _normalize_for_match(n)}

    matched = []
    for norm, orig in toc_norm.items():
        if norm in ext_norm:
            matched.append(orig)

    missing = [toc_norm[n] for n in toc_norm if n not in ext_norm]
    extra = [ext_norm[n] for n in ext_norm if n not in toc_norm]

    return matched, missing, extra
