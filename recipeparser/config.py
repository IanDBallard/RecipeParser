"""
Central configuration constants for the recipeparser package.

All tuneable values live here so that CLI arguments, environment variable
overrides, or future config-file loading only need to touch one place.
"""

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

# Maximum Gemini API calls in-flight at once.
# Gemini free-tier: ~5; increase for paid tiers via --concurrency CLI flag.
MAX_CONCURRENT_API_CALLS: int = 5

# Wall-clock seconds to wait for a single segment/categorisation future.
SEGMENT_TIMEOUT_SECS: int = 300
