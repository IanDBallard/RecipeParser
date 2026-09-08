"""
Central configuration constants for the recipeparser package.

All tuneable values live here so that CLI arguments, environment variable
overrides, or future config-file loading only need to touch one place.
"""
import os
from typing import Optional

# ---------------------------------------------------------------------------
# EPUB / chunking
# ---------------------------------------------------------------------------

# Images smaller than this are treated as decorative separators/icons.
# Real recipe photos are consistently >= 20 KB; separators are typically 2-14 KB.
MIN_PHOTO_BYTES: int = 20_000

# Maximum characters per text chunk sent to the LLM.
# GEMINI_MODEL has a large context window, but very long chapters inflate
# latency. ~30 k chars ≈ ~7-8 k tokens.
MAX_CHUNK_CHARS: int = 30_000

# A non-recipe "image-only" chunk is injected as a HERO IMAGE breadcrumb into
# the following chunk only when its non-image text is shorter than this.
HERO_INJECT_MAX_STUB_CHARS: int = 120

# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------

# The generation model behind every extraction, refinement, categorisation,
# TOC and vision-OCR call. gemini-2.5-flash retires 2026-10-16; this points
# at its GA successor. Override with GEMINI_MODEL to test another model
# without a code change.
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

# The embedding model behind /embed and the recipe-embedding pipeline stage.
# Not implicated in gemini-2.5-flash's retirement — tracked separately.
GEMINI_EMBEDDING_MODEL: str = os.environ.get(
    "GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001"
)

# Every call this package makes is a bounded extraction, refinement or
# classification task with one correct JSON (or plain-text) answer, not
# open-ended reasoning — thinking tokens buy nothing here and bill at the
# output rate. Disabled by default; set GEMINI_THINKING_BUDGET to a positive
# token count to re-enable it for a specific investigation.
THINKING_BUDGET: int = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))

# Per-call HTTP timeout passed to generate_content (seconds).
HTTP_TIMEOUT_SECS: int = 180

# Maximum retries on 429 / quota errors before giving up.
MAX_RETRIES: int = 5

# A reply that will not parse is not a rate-limit signal: the transport
# succeeded and the model simply returned truncated or malformed JSON. The
# 2026-09-04 import showed such replies parse cleanly when asked again, so a
# short, flat retry recovers them without adding the quota ladder's minutes to
# every bad chunk of a large import.
MAX_PARSE_RETRIES: int = 2
PARSE_RETRY_DELAY_SECS: float = 1.0

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

# No SEGMENT_TIMEOUT_SECS here any more. It named a per-chunk wall-clock bound
# the pipeline never enforced: as_completed() only yields finished futures, so
# the future.result(timeout=...) it fed could not block, and a running thread
# cannot be cancelled in any case. The bound that does exist is per API call —
# HTTP_TIMEOUT_SECS above, applied in gemini._call_with_retry.

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

# ---------------------------------------------------------------------------
# Phase 3 — Pipeline control and rate-limit auto-pause
# ---------------------------------------------------------------------------

# Number of consecutive HTTP 429 responses before the pipeline auto-pauses.
RATE_LIMIT_PAUSE_THRESHOLD: int = 3

# Seconds to wait before auto-resuming after a rate-limit pause (default 12 h).
RATE_LIMIT_AUTO_RESUME_SECS: int = 43_200

# Subdirectory name (relative to output_dir) where checkpoint JSON files are stored.
CHECKPOINT_SUBDIR: str = ".recipeparser_checkpoints"

# ---------------------------------------------------------------------------
# Live-write guard
# ---------------------------------------------------------------------------
#
# Lives here rather than in adapters/ or io/ because both a write path in
# recipeparser/io/writers/supabase.py and a client-factory path in
# recipeparser/adapters/api.py need to call it, and io/ must never import
# from adapters/ (hexagonal architecture — see the ruff banned-api rule).
# This module is neutral ground both are already allowed to import from.


def live_writes_blocked() -> bool:
    """True when this process is a test run that must not touch a real project.

    The service key sits in .env, so an ordinary `pytest` run picked it up and wrote
    ingestion_jobs rows into the live database: eight of them on 2026-09-04, four left
    at status "running" because the process ended mid-job, which the Cayenne client
    then displayed forever as jobs in progress. Nothing here needs a real project to
    be under test, so the writes are refused rather than the credentials removed --
    a developer who wants the opposite sets ALLOW_LIVE_WRITES_IN_TESTS=1 and means it.
    """
    if os.environ.get("ALLOW_LIVE_WRITES_IN_TESTS") == "1":
        return False
    return "PYTEST_CURRENT_TEST" in os.environ
