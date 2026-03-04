"""
Central configuration constants for the recipeparser package.

All tuneable values live here so that CLI arguments, environment variable
overrides, or future config-file loading only need to touch one place.
"""
from typing import Optional

# ---------------------------------------------------------------------------
# EPUB / chunking
# ---------------------------------------------------------------------------

# Images smaller than this are treated as decorative separators/icons.
# Real recipe photos are consistently >= 20 KB; separators are typically 2-14 KB.
MIN_PHOTO_BYTES: int = 20_000

# Maximum characters per text chunk sent to the LLM.
# gemini-2.5-flash has a large context window, but very long chapters inflate
# latency. ~30 k chars ≈ ~7-8 k tokens.
MAX_CHUNK_CHARS: int = 30_000

# A non-recipe "image-only" chunk is injected as a HERO IMAGE breadcrumb into
# the following chunk only when its non-image text is shorter than this.
HERO_INJECT_MAX_STUB_CHARS: int = 120

# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------

# Per-call HTTP timeout passed to generate_content (seconds).
HTTP_TIMEOUT_SECS: int = 180

# Maximum retries on 429 / quota errors before giving up.
MAX_RETRIES: int = 5

# Initial exponential back-off delay (seconds); doubles after each retry,
# capped at BACKOFF_MAX_SECS.
BACKOFF_BASE_SECS: float = 2.0
BACKOFF_MAX_SECS: float = 120.0

# ---------------------------------------------------------------------------
# Pipeline concurrency
# ---------------------------------------------------------------------------

# Maximum Gemini API calls in-flight at once (default when not overridden by CLI/GUI).
# Gemini free tier = 5 requests per minute; use 1 and spacing (below) to stay under.
MAX_CONCURRENT_API_CALLS: int = 1

# Hard cap on concurrency (--concurrency and GUI are clamped to this).
# Google docs cite rate limits (RPM) rather than a concurrency number; 10 is a
# conservative per-key ceiling to stay within typical RPM.
MAX_CONCURRENT_CAP: int = 10

# When rpm is not set and concurrency is 1, wait this long between requests
# to stay under free-tier 5 requests/minute.
FREE_TIER_DELAY_SECS: float = 12.0

# Wall-clock seconds to wait for a single segment/categorisation future.
SEGMENT_TIMEOUT_SECS: int = 300

# ---------------------------------------------------------------------------
# TOC extraction and chunking (Phase 2)
# ---------------------------------------------------------------------------

# Fewer than this = treat as no TOC, fall back to raw chunks.
MIN_TOC_ENTRIES: int = 2

# Fraction of TOC entries that must be classified as recipe names (not section headers).
# Below this = fall back to raw chunking.
MIN_TOC_RECIPE_RATIO: float = 0.5

# Fraction of TOC titles that must be found in text to use TOC-driven chunking.
# Below this = fall back to raw chunks.
MIN_TOC_MATCH_RATIO: float = 0.3

# Number of front-matter pages to scan for AI TOC parsing when PDF outline is empty.
# Many cookbooks place the TOC on pages 5-8; scanning 10 pages covers typical layouts.
TOC_PDF_FRONT_MATTER_PAGES: int = 10

# ---------------------------------------------------------------------------
# PDF pre-flight (Phase 1)
# ---------------------------------------------------------------------------

# Below this average chars per page (over first N pages), PDF is treated as no text layer / scan.
PDF_PREFLIGHT_MIN_CHARS_PER_PAGE: int = 100
PDF_PREFLIGHT_SAMPLE_PAGES: int = 5
PDF_PREFLIGHT_MIN_PAGES: int = 1  # Reject if 0 pages; warn if below this (e.g. pamphlet).
PDF_PREFLIGHT_MAX_PAGES: Optional[int] = 2000  # Optional cap to avoid runaway cost; None = no cap.
