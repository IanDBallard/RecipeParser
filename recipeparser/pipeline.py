"""
Top-level orchestration: open book (EPUB or PDF) → extract → categorise → export.

Segment extraction and categorisation both run in parallel using a
ThreadPoolExecutor capped by a semaphore so we never exceed Gemini's
concurrent-call limit.  Each future is given an individual timeout;
timed-out or failed segments are logged and skipped rather than aborting
the whole run.
"""
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from recipeparser import categories as cat_module
from recipeparser import gemini as gem
from recipeparser.config import (
    FREE_TIER_DELAY_SECS,
    HERO_INJECT_MAX_STUB_CHARS,
    MAX_CONCURRENT_API_CALLS,
    MAX_CONCURRENT_CAP,
    MIN_TOC_ENTRIES,
    SEGMENT_TIMEOUT_SECS,
)
from recipeparser.epub import is_recipe_candidate, load_epub, split_large_chunk
from recipeparser.pdf import load_pdf
from recipeparser.exceptions import (
    EpubExtractionError,
    ExportError,
    GeminiConnectionError,
    PdfExtractionError,
)
from recipeparser.export import create_paprika_export
from recipeparser.models import RecipeExtraction
from recipeparser.toc import extract_toc_epub, extract_toc_pdf, run_recon

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum class (design §5.9): current pipeline state for logging and GUI
# ---------------------------------------------------------------------------

class Stage(Enum):
    LOAD = "load"
    PREFLIGHT = "preflight"
    TOC_EXTRACT = "toc_extract"
    CHUNK_TOC = "chunk_toc"
    CHUNK_RAW = "chunk_raw"
    EXTRACT = "extract"
    RECON = "recon"
    EXPORT = "export"


class ChunkingPath(Enum):
    TOC_DRIVEN = "toc_driven"
    RAW_CHUNKS = "raw_chunks"


class ReconStatus(Enum):
    SKIPPED = "skipped"
    PENDING = "pending"
    DONE = "done"


class PreflightOutcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass
class PipelineState:
    """Current state variables; set at each pipeline step."""
    stage: Stage = Stage.LOAD
    chunking_path: Optional[ChunkingPath] = None
    recon_status: ReconStatus = ReconStatus.SKIPPED
    preflight_outcome: Optional[PreflightOutcome] = None


class _RPMRateLimiter:
    """Thread-safe limiter: at most `rpm` request starts per 60-second window."""

    def __init__(self, rpm: int) -> None:
        self._rpm = rpm
        self._lock = threading.Lock()
        self._starts: List[float] = []

    def wait_then_record_start(self) -> None:
        while True:
            now = time.monotonic()
            with self._lock:
                cutoff = now - 60.0
                self._starts = [t for t in self._starts if t > cutoff]
                if len(self._starts) < self._rpm:
                    self._starts.append(now)
                    return
                sleep_until = min(self._starts) + 60.0 - now
            delay = max(0.0, sleep_until)
            if delay > 0:
                time.sleep(delay)


@dataclass
class PipelineContext:
    """
    Bundles all shared state and dependencies for worker threads.
    """
    client: object
    semaphore: threading.Semaphore
    units: str
    category_tree: list
    paprika_cats: list
    # Fixed delay between request starts when rpm is not set and concurrency is 1.
    min_interval_secs: Optional[float] = None
    # When set, workers call this before each API call to enforce requests-per-minute.
    rate_limiter: Optional[_RPMRateLimiter] = None


def _process_segment(
    index: int,
    chunk: str,
    ctx: PipelineContext,
) -> Tuple[int, List[RecipeExtraction]]:
    """
    Worker executed in a thread pool.  Acquires the semaphore, then enforces
    RPM (if set) or min_interval before calling the API.
    """
    with ctx.semaphore:
        if ctx.rate_limiter:
            ctx.rate_limiter.wait_then_record_start()
        elif ctx.min_interval_secs:
            time.sleep(ctx.min_interval_secs)
        if gem.needs_table_normalisation(chunk):
            log.info(
                "  Segment %d: Baker's %% table detected — normalising...", index
            )
            chunk = gem.normalise_baker_table(chunk, ctx.client)

        result = gem.extract_recipes(chunk, ctx.client, units=ctx.units)

    if result and result.recipes:
        log.info("  Segment %d: %d recipe(s) found.", index, len(result.recipes))
        return index, list(result.recipes)

    log.info("  Segment %d: no recipes extracted.", index)
    return index, []


def _categorise_one(
    recipe: RecipeExtraction,
    ctx: PipelineContext,
) -> Tuple[RecipeExtraction, List[str]]:
    with ctx.semaphore:
        if ctx.rate_limiter:
            ctx.rate_limiter.wait_then_record_start()
        elif ctx.min_interval_secs:
            time.sleep(ctx.min_interval_secs)
        cats = cat_module.categorise_recipe(
            recipe, ctx.category_tree, ctx.paprika_cats, ctx.client
        )
    return recipe, cats


def deduplicate_recipes(recipes: List[RecipeExtraction]) -> List[RecipeExtraction]:
    """
    Remove duplicate recipes based on a normalised version of the name.
    Keeps the first occurrence (preserves chapter order).
    """
    seen: set = set()
    unique: List[RecipeExtraction] = []

    for recipe in recipes:
        key = recipe.name.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(recipe)
        else:
            log.info("Duplicate recipe skipped: '%s'", recipe.name)

    return unique


def process_epub(
    epub_path: str,
    output_dir: str,
    client,
    units: str = "book",
    concurrency: Optional[int] = None,
    rpm: Optional[int] = None,
) -> str:
    """
    Full pipeline: open EPUB → extract images + text → parallel Gemini calls
    → deduplicate → categorise → export to .paprikarecipes.

    concurrency: max in-flight API calls (default from config), capped at 10.
    rpm: optional requests-per-minute limit; when set, constrains how many
    requests can start in any 60s window (e.g. rpm=10 and concurrency=10
    with all finishing in 10s → sleep 50s before next batch).
    """
    cap = concurrency if concurrency is not None else MAX_CONCURRENT_API_CALLS
    cap = min(cap, MAX_CONCURRENT_CAP)
    if rpm is not None and rpm > 0:
        rate_limiter: Optional[_RPMRateLimiter] = _RPMRateLimiter(rpm)
        min_interval = None
    else:
        rate_limiter = None
        min_interval = FREE_TIER_DELAY_SECS if cap == 1 else None

    os.makedirs(output_dir, exist_ok=True)

    log.info("Verifying Gemini API connectivity...")
    if not gem.verify_connectivity(client):
        raise GeminiConnectionError(
            "Gemini API unreachable — fix the API key or enable the "
            "Generative Language API and retry."
        )

    pipeline_state = PipelineState()
    book_path = epub_path  # param name kept for backward compatibility
    ext = os.path.splitext(book_path)[1].lower()

    pipeline_state.stage = Stage.LOAD
    log.info("Opening book: %s", book_path)
    try:
        if ext == ".epub":
            book_source, image_dir, qualifying_images, raw_chunks = load_epub(book_path, output_dir)
            pipeline_state.chunking_path = ChunkingPath.RAW_CHUNKS
            pipeline_state.recon_status = ReconStatus.SKIPPED
        elif ext == ".pdf":
            pipeline_state.stage = Stage.PREFLIGHT
            book_source, image_dir, qualifying_images, raw_chunks = load_pdf(book_path, output_dir)
            pipeline_state.preflight_outcome = PreflightOutcome.PASS
            pipeline_state.chunking_path = ChunkingPath.RAW_CHUNKS
            pipeline_state.recon_status = ReconStatus.SKIPPED
        else:
            raise EpubExtractionError(
                f"Unsupported format: '{ext}'. Use .epub or .pdf."
            )
    except (EpubExtractionError, PdfExtractionError):
        raise
    except Exception as e:
        if ext == ".pdf":
            raise PdfExtractionError(f"Failed to load PDF '{book_path}': {e}") from e
        raise EpubExtractionError(f"Failed to load EPUB '{book_path}': {e}") from e

    log.info("Book source: %s", book_source)

    # --- TOC extraction (for recon only; chunking stays page/document-based) ----
    toc_entries: List[Tuple[str, Optional[int]]] = []
    pipeline_state.stage = Stage.TOC_EXTRACT

    if ext == ".epub":
        toc_entries = extract_toc_epub(book_path, raw_chunks, client)
    else:
        toc_entries = extract_toc_pdf(book_path, raw_chunks, client)

    if len(toc_entries) >= MIN_TOC_ENTRIES:
        pipeline_state.recon_status = ReconStatus.PENDING
        log.info("TOC extracted: %d entries (for recon; using raw chunking).", len(toc_entries))
    else:
        pipeline_state.recon_status = ReconStatus.SKIPPED

    pipeline_state.stage = Stage.CHUNK_RAW
    pipeline_state.chunking_path = ChunkingPath.RAW_CHUNKS

    chunks: List[str] = []
    for raw in raw_chunks:
        chunks.extend(split_large_chunk(raw))

    # --- Hero-image look-ahead injection -----------------------------------------
    # Some books place the hero photo on a standalone page immediately before the
    # recipe text.  That tiny chunk fails is_recipe_candidate, so Gemini never
    # sees the image marker.  We detect it and prepend it to the next chunk as
    # [HERO IMAGE:] — the extraction prompt treats this as a definitive signal.
    _IMAGE_ONLY_RE = re.compile(r"^\s*\[IMAGE:\s*([^\]]+)\]\s*", re.MULTILINE)

    enriched: List[str] = list(chunks)
    for i, chunk in enumerate(chunks):
        if not is_recipe_candidate(chunk):
            markers = _IMAGE_ONLY_RE.findall(chunk)
            non_img_text = _IMAGE_ONLY_RE.sub("", chunk).strip()
            if markers and len(non_img_text) < HERO_INJECT_MAX_STUB_CHARS and i + 1 < len(enriched):
                hero_marker = f"[HERO IMAGE: {markers[-1].strip()}]\n"
                enriched[i + 1] = hero_marker + enriched[i + 1]
                log.debug(
                    "Injected hero image '%s' from chunk %d into chunk %d.",
                    markers[-1].strip(), i, i + 1,
                )

    candidate_chunks = [
        (i, chunk) for i, chunk in enumerate(enriched) if is_recipe_candidate(chunk)
    ]
    pipeline_state.stage = Stage.EXTRACT
    log.info(
        "Total segments: %d  |  Recipe candidates: %d  |  Units preference: %s",
        len(enriched),
        len(candidate_chunks),
        units,
    )
    if rpm is not None and rpm > 0:
        log.info("Rate limit: %d requests/minute (concurrency cap: %d).", rpm, cap)
    else:
        log.info("Concurrency cap: %d (no RPM limit).", cap)

    # Build the shared context object once — passed into every worker.
    category_tree = cat_module.load_category_tree()
    paprika_cats = cat_module.build_paprika_categories(category_tree)
    ctx = PipelineContext(
        client=client,
        semaphore=threading.Semaphore(cap),
        units=units,
        category_tree=category_tree,
        paprika_cats=paprika_cats,
        min_interval_secs=min_interval,
        rate_limiter=rate_limiter,
    )

    # --- Parallel extraction -----------------------------------------------------
    results: Dict[int, List[RecipeExtraction]] = {}

    with ThreadPoolExecutor(max_workers=cap) as executor:
        future_to_index: Dict[Future, int] = {
            executor.submit(_process_segment, idx, chunk, ctx): idx
            for idx, chunk in candidate_chunks
        }
        log.info(
            "Submitted %d segment(s) to thread pool (concurrency cap: %d).",
            len(future_to_index),
            cap,
        )
        for future in as_completed(future_to_index):
            seg_idx = future_to_index[future]
            try:
                idx, recipes = future.result(timeout=SEGMENT_TIMEOUT_SECS)
                results[idx] = recipes
            except TimeoutError:
                log.warning(
                    "Segment %d timed out after %ds — skipping.",
                    seg_idx, SEGMENT_TIMEOUT_SECS,
                )
                results[seg_idx] = []
            except Exception as exc:
                log.error("Segment %d raised an error: %s — skipping.", seg_idx, exc)
                results[seg_idx] = []

    # Reassemble in chapter order
    all_recipes: List[RecipeExtraction] = []
    for idx in sorted(results):
        all_recipes.extend(results[idx])

    num_before_dedup = len(all_recipes)
    log.info("Total recipes before deduplication: %d", num_before_dedup)
    all_recipes = deduplicate_recipes(all_recipes)
    log.info("Total recipes after deduplication:  %d", len(all_recipes))

    recon_toc = recon_extracted = recon_matched = recon_missing = recon_extra = 0

    # --- Recon (when TOC was extracted) ------------------------------------------
    if toc_entries and pipeline_state.recon_status == ReconStatus.PENDING:
        pipeline_state.stage = Stage.RECON
        extracted_names = [r.name for r in all_recipes]
        matched, missing, extra = run_recon(toc_entries, extracted_names)
        recon_toc = len(toc_entries)
        recon_extracted = len(extracted_names)
        recon_matched = len(matched)
        recon_missing = len(missing)
        recon_extra = len(extra)
        pipeline_state.recon_status = ReconStatus.DONE
        log.info(
            "Recon: TOC %d  |  Extracted %d  |  Matched %d  |  Missing %d  |  Extra %d",
            len(toc_entries), len(extracted_names), len(matched), len(missing), len(extra),
        )
        if missing:
            log.info("  Missing from extraction: %s", missing[:15])
        if extra:
            log.info("  Extra (not in TOC): %s", extra[:10])

    # --- Parallel categorisation -------------------------------------------------
    log.info("Categorising %d recipe(s) in parallel...", len(all_recipes))

    with ThreadPoolExecutor(max_workers=cap) as cat_executor:
        cat_futures: Dict[Future, RecipeExtraction] = {
            cat_executor.submit(_categorise_one, recipe, ctx): recipe
            for recipe in all_recipes
        }
        for future in as_completed(cat_futures):
            r = cat_futures[future]
            try:
                recipe, cats = future.result(timeout=SEGMENT_TIMEOUT_SECS)
                recipe.categories = cats
                log.info("  %-40s -> %s", recipe.name[:40], cats)
            except TimeoutError:
                log.warning("  Categorisation timed out for '%s' — using fallback.", r.name)
                r.categories = ["EPUB Imports"]
            except Exception as exc:
                log.error("  Categorisation error for '%s': %s — using fallback.", r.name, exc)
                r.categories = ["EPUB Imports"]

    # --- Photo assignment summary ------------------------------------------------
    with_photo = sum(1 for r in all_recipes if r.photo_filename)
    log.info(
        "Photo assignment: %d / %d recipes have a photo filename assigned.",
        with_photo, len(all_recipes),
    )
    if with_photo < len(all_recipes):
        missing = [r.name for r in all_recipes if not r.photo_filename]
        log.info("  No photo assigned for: %s", ", ".join(missing[:10]))
        if len(missing) > 10:
            log.info("  ... and %d more.", len(missing) - 10)

    # --- Export ------------------------------------------------------------------
    pipeline_state.stage = Stage.EXPORT
    book_stem = os.path.splitext(os.path.basename(book_path))[0]
    export_filename = f"{book_stem}.paprikarecipes"

    success = create_paprika_export(
        all_recipes, output_dir, image_dir, export_filename, book_source
    )

    if success and os.path.exists(image_dir):
        log.info("Cleaning up temporary image directory...")
        shutil.rmtree(image_dir)
        log.info("Cleanup complete.")
    elif not success:
        log.warning(
            "Export failed or empty — keeping image directory for inspection: %s",
            image_dir,
        )
        raise ExportError(
            f"No recipes were exported from '{book_path}'. "
            "Check the log for extraction details."
        )

    export_path = os.path.join(output_dir, export_filename)

    # --- Run summary -------------------------------------------------------------
    fmt = ext.upper().lstrip(".")
    toc_line = f"{len(toc_entries)} entries" if toc_entries else "none"
    log.info("")
    log.info("--- Run summary ---")
    log.info("Load:    %s (%s)", book_source, fmt)
    log.info("TOC:     %s", toc_line)
    log.info("Chunk:   %d segments, %d recipe candidates", len(enriched), len(candidate_chunks))
    log.info("Extract: %d recipes → %d after deduplication", num_before_dedup, len(all_recipes))
    if recon_toc:
        log.info(
            "Recon:   TOC %d | Extracted %d | Matched %d | Missing %d | Extra %d",
            recon_toc, recon_extracted, recon_matched, recon_missing, recon_extra,
        )
    log.info(
        "Export:  %d recipes → %s",
        len(all_recipes), export_path,
    )

    return export_path
