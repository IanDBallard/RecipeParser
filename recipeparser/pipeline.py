"""
Top-level orchestration: open book (EPUB or PDF) → extract → categorise → export.

Segment extraction and categorisation both run in parallel using a
ThreadPoolExecutor capped by a semaphore so we never exceed Gemini's
concurrent-call limit.  Each future is given an individual timeout;
timed-out or failed segments are logged and skipped rather than aborting
the whole run.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from recipeparser import categories as cat_module
from recipeparser import gemini as gem
from recipeparser.config import (
    CHECKPOINT_SUBDIR,
    FREE_TIER_DELAY_SECS,
    HERO_INJECT_MAX_STUB_CHARS,
    MAX_CONCURRENT_API_CALLS,
    MAX_CONCURRENT_CAP,
    MIN_TOC_ENTRIES,
    RATE_LIMIT_AUTO_RESUME_SECS,
    RATE_LIMIT_PAUSE_THRESHOLD,
    SEGMENT_TIMEOUT_SECS,
)
from recipeparser.epub import is_recipe_candidate, load_epub, split_large_chunk
from recipeparser.pdf import load_pdf
from recipeparser.exceptions import (
    CheckpointError,
    EpubExtractionError,
    ExportError,
    GeminiConnectionError,
    PdfExtractionError,
    PipelineTransitionError,
    RateLimitPauseError,
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
    controller: Optional["PipelineController"] = None


def _process_segment(
    index: int,
    chunk: str,
    ctx: PipelineContext,
) -> Tuple[int, List[RecipeExtraction]]:
    """
    Worker executed in a thread pool.  Acquires the semaphore, then enforces
    RPM (if set) or min_interval before calling the API.
    """
    if ctx.controller and not ctx.controller.check_pause_point():
        raise PipelineTransitionError("Pipeline cancelled")

    with ctx.semaphore:
        if ctx.rate_limiter:
            ctx.rate_limiter.wait_then_record_start()
        elif ctx.min_interval_secs:
            time.sleep(ctx.min_interval_secs)
            
        try:
            if gem.needs_table_normalisation(chunk):
                log.info(
                    "  Segment %d: Baker's %% table detected — normalising...", index
                )
                chunk = gem.normalise_baker_table(chunk, ctx.client)

            result = gem.extract_recipes(chunk, ctx.client, units=ctx.units)
            if ctx.controller:
                ctx.controller.reset_429_counter()
        except Exception as exc:
            if ctx.controller and gem._is_rate_limit_error(exc):
                try:
                    ctx.controller.record_429()
                except RateLimitPauseError:
                    ctx.controller.trigger_rate_limit_pause()
            raise

    if result and result.recipes:
        log.info("  Segment %d: %d recipe(s) found.", index, len(result.recipes))
        return index, list(result.recipes)

    log.info("  Segment %d: no recipes extracted.", index)
    return index, []


def _categorise_one(
    recipe: RecipeExtraction,
    ctx: PipelineContext,
) -> Tuple[RecipeExtraction, List[str]]:
    if ctx.controller and not ctx.controller.check_pause_point():
        raise PipelineTransitionError("Pipeline cancelled")

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
    controller: Optional["PipelineController"] = None,
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

    if controller:
        controller.transition("start")

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
        controller=controller,
    )

    # --- Checkpoint Loading ------------------------------------------------------
    results: Dict[int, List[RecipeExtraction]] = {}
    completed_segments = set()
    
    if controller:
        cp = controller.load_checkpoint(book_path)
        if cp and cp.get("stage") == Stage.EXTRACT.value:
            completed_segments = set(cp.get("completed_segments", []))
            for item in cp.get("extracted_recipes", []):
                idx = item.get("segment")
                recs = [RecipeExtraction(**r) for r in item.get("recipes", [])]
                if idx is not None:
                    results[idx] = recs
            log.info("Resumed from checkpoint: %d segments already completed.", len(completed_segments))

    # --- Parallel extraction -----------------------------------------------------
    tasks_to_run = [(idx, chunk) for idx, chunk in candidate_chunks if idx not in completed_segments]
    
    with ThreadPoolExecutor(max_workers=cap) as executor:
        future_to_index: Dict[Future, int] = {
            executor.submit(_process_segment, idx, chunk, ctx): idx
            for idx, chunk in tasks_to_run
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
            except PipelineTransitionError:
                log.warning("Pipeline cancelled during segment %d.", seg_idx)
                continue
            except Exception as exc:
                log.error("Segment %d raised an error: %s — skipping.", seg_idx, exc)
                results[seg_idx] = []

            # Record completion and save checkpoint regardless of success/fail
            completed_segments.add(seg_idx)
            if controller:
                # Cooperative pause point between segments (in the orchestrator
                # thread) so that a pause request is honoured even when the
                # worker thread is blocked inside a long API call.
                #
                # We check for both PAUSING and PAUSED because the worker's own
                # check_pause_point() may have already transitioned PAUSING→PAUSED
                # before we get here.  In that case we must still wait on
                # _resume_event so the worker (which is blocked inside
                # check_pause_point) can be unblocked by request_resume().
                with controller._lock:
                    orch_status = controller.status
                if orch_status == PipelineStatus.PAUSING:
                    controller.transition("paused")
                    log.info("PipelineController: pipeline paused (orchestrator) — waiting for resume or cancel.")
                    controller._resume_event.wait()
                    with controller._lock:
                        post_status = controller.status
                    if post_status == PipelineStatus.CANCELLING:
                        break
                    controller.transition("running")
                elif orch_status == PipelineStatus.PAUSED:
                    # Worker already transitioned to PAUSED via check_pause_point();
                    # just wait here until request_resume() sets the event.
                    log.info("PipelineController: orchestrator waiting while worker is paused.")
                    controller._resume_event.wait()
                    with controller._lock:
                        post_status = controller.status
                    if post_status == PipelineStatus.CANCELLING:
                        break
                    # Worker's check_pause_point() will call transition("running")
                    # when it unblocks — no need to do it here.

                controller.save_checkpoint(
                    book_path=book_path,
                    stage=Stage.EXTRACT.value,
                    completed_segments=list(completed_segments),
                    extracted_recipes=[
                        {"segment": k, "recipes": [r.model_dump() for r in v]}
                        for k, v in results.items()
                    ],
                    toc_entries=[t.model_dump() if hasattr(t, 'model_dump') else t for t in toc_entries] if toc_entries else [],
                )

    if controller and controller.status == PipelineStatus.CANCELLING:
        log.info("Pipeline cancelled by user. Exiting early.")
        return ""

    # Reassemble in chapter order
    all_recipes: List[RecipeExtraction] = []
    for idx in sorted(results):
        all_recipes.extend(results[idx])

    num_before_dedup = len(all_recipes)
    log.info("Total recipes before deduplication: %d", num_before_dedup)
    all_recipes = deduplicate_recipes(all_recipes)
    log.info("Total recipes after deduplication:  %d", len(all_recipes))

    recon_toc = recon_extracted = recon_matched = recon_missing = recon_extra = 0
    recon_missing_list: List[str] = []
    recon_extra_list: List[str] = []

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
        recon_missing_list = missing
        recon_extra_list = extra
        pipeline_state.recon_status = ReconStatus.DONE
        log.info(
            "Recon: TOC %d  |  Extracted %d  |  Matched %d  |  Missing %d  |  Extra %d",
            len(toc_entries), len(extracted_names), len(matched), len(missing), len(extra),
        )
        if missing:
            log.info("  Missing from extraction: %s", missing)
        if extra:
            log.info("  Extra (not in TOC): %s", extra)

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
            except PipelineTransitionError:
                log.warning("Pipeline cancelled during categorisation.")
                break
            except Exception as exc:
                log.error("  Categorisation error for '%s': %s — using fallback.", r.name, exc)
                r.categories = ["EPUB Imports"]

    if controller and controller.status == PipelineStatus.CANCELLING:
        log.info("Pipeline cancelled by user during categorisation. Exiting early.")
        return "" 

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
        
    if success and controller:
        controller.delete_checkpoint(book_path)
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
        if recon_missing_list:
            log.info("  Missing from extraction: %s", recon_missing_list)
        if recon_extra_list:
            log.info("  Extra (not in TOC): %s", recon_extra_list)
    log.info(
        "Export:  %d recipes → %s",
        len(all_recipes), export_path,
    )

    if controller:
        controller.transition("done")

    return export_path


def run_cayenne_pipeline(
    source_text: str,
    client,
    uom_system: str = "US",
    measure_preference: str = "Volume",
    source_url: Optional[str] = None,
) -> "IngestResponse":
    """
    Stateless high-fidelity pipeline for Project Cayenne:
    1. Extract raw recipe(s) from text.
    2. Refine the first recipe found into Cayenne format (Fat Tokens + UOM).
    3. Generate 1536-dim vector embedding of 'title + ingredient names'.

    Returns an IngestResponse (defined in models.py).
    Raises RuntimeError or ValueError on pipeline failure.
    """
    from recipeparser.models import CayenneRecipe, IngestResponse
    from recipeparser.gemini import (
        extract_recipe_from_text,
        refine_recipe_for_cayenne,
        get_embeddings,
    )

    recipe_list = extract_recipe_from_text(source_text, client)
    if not recipe_list or not recipe_list.recipes:
        raise ValueError("No recipes found in source text.")

    raw_recipe = recipe_list.recipes[0]

    refined = refine_recipe_for_cayenne(
        raw_recipe,
        client,
        uom_system=uom_system,
        measure_preference=measure_preference,
    )
    if not refined:
        raise RuntimeError("Refinement pass failed.")

    # Vectorize for semantic search: title + ingredient names
    ing_names = ", ".join([i.name for i in refined.structured_ingredients])
    embedding_input = f"{refined.title}\n\n{ing_names}"
    embedding = get_embeddings(embedding_input, client)

    cayenne_recipe = CayenneRecipe(
        title=refined.title,
        prep_time=raw_recipe.prep_time,
        cook_time=raw_recipe.cook_time,
        base_servings=refined.base_servings or 4,
        source_url=source_url,
        categories=["Uncategorized"],
        structured_ingredients=refined.structured_ingredients,
        tokenized_directions=refined.tokenized_directions,
    )

    return IngestResponse(**cayenne_recipe.model_dump(), embedding=embedding)


# ---------------------------------------------------------------------------
# Phase 3b/3c — PipelineController FSM with checkpoint and auto-pause
# ---------------------------------------------------------------------------

class PipelineStatus(Enum):
    """FSM states for the pipeline controller."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    CANCELLING = "cancelling"


# Valid (current_state, event) → next_state transitions
_TRANSITIONS: Dict[Tuple[PipelineStatus, str], PipelineStatus] = {
    (PipelineStatus.IDLE,       "start"):   PipelineStatus.RUNNING,
    (PipelineStatus.RUNNING,    "pause"):   PipelineStatus.PAUSING,
    (PipelineStatus.PAUSING,    "paused"):  PipelineStatus.PAUSED,
    (PipelineStatus.PAUSED,     "resume"):  PipelineStatus.RESUMING,
    (PipelineStatus.RESUMING,   "running"): PipelineStatus.RUNNING,
    (PipelineStatus.RUNNING,    "cancel"):  PipelineStatus.CANCELLING,
    (PipelineStatus.PAUSING,    "cancel"):  PipelineStatus.CANCELLING,
    (PipelineStatus.PAUSED,     "cancel"):  PipelineStatus.CANCELLING,
    (PipelineStatus.RUNNING,    "done"):    PipelineStatus.IDLE,
    (PipelineStatus.RUNNING,    "error"):   PipelineStatus.IDLE,
    (PipelineStatus.PAUSING,    "error"):   PipelineStatus.IDLE,
    (PipelineStatus.RESUMING,   "error"):   PipelineStatus.IDLE,
}

_CHECKPOINT_VERSION = 1


class PipelineController:
    """
    Finite-state machine that wraps a pipeline run with pause/resume/cancel
    support, checkpoint persistence, and auto-pause on repeated 429 errors.

    Thread safety
    -------------
    ``transition()`` and ``request_pause()`` / ``request_cancel()`` may be
    called from any thread (e.g. the GUI thread).  Internal state is protected
    by ``_lock``.  The pipeline worker thread calls ``check_pause_point()``
    cooperatively between segments.

    Checkpoint format (version 1)
    ------------------------------
    A JSON file at ``<output_dir>/<CHECKPOINT_SUBDIR>/<book_hash>.json``::

        {
          "version": 1,
          "book_path": "/path/to/book.epub",
          "book_hash": "sha256:...",
          "stage": "EXTRACT",
          "completed_segments": [0, 1, 2],
          "extracted_recipes": [],
          "toc_entries": [],
          "timestamp": "2026-03-08T14:00:00Z"
        }
    """

    def __init__(self, output_dir: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self.status: PipelineStatus = PipelineStatus.IDLE
        self._consecutive_429s: int = 0
        self._output_dir: Optional[Path] = Path(output_dir) if output_dir else None
        # Event used to block the worker thread while paused
        self._resume_event = threading.Event()
        self._resume_event.set()  # not paused initially
        # Timer handle for auto-resume after rate-limit pause
        self._auto_resume_timer: Optional[threading.Timer] = None

    # ── FSM ───────────────────────────────────────────────────────────────────

    def transition(self, event: str) -> bool:
        """
        Attempt a state transition triggered by ``event``.

        Returns True if the transition was valid and applied; False if the
        (current_state, event) pair has no defined transition (logs a warning).
        """
        with self._lock:
            key = (self.status, event)
            if key in _TRANSITIONS:
                old = self.status
                self.status = _TRANSITIONS[key]
                log.debug(
                    "PipelineController: %s --%s--> %s", old.value, event, self.status.value
                )
                return True
            log.warning(
                "PipelineController: invalid transition (%s, '%s') — ignored.",
                self.status.value, event,
            )
            return False

    def transition_or_raise(self, event: str) -> None:
        """Like ``transition()`` but raises ``PipelineTransitionError`` on failure."""
        if not self.transition(event):
            with self._lock:
                current = self.status.value
            raise PipelineTransitionError(
                f"No transition defined for state='{current}' event='{event}'."
            )

    # ── Pause / resume / cancel (called from GUI thread) ─────────────────────

    def request_pause(self) -> bool:
        """Signal the worker to pause at the next cooperative check point."""
        ok = self.transition("pause")
        if ok:
            self._resume_event.clear()
        return ok

    def request_resume(self) -> bool:
        """Resume a paused pipeline."""
        ok = self.transition("resume")
        if ok:
            self._cancel_auto_resume_timer()
            self._resume_event.set()
            # Worker will call transition("running") once it unblocks
        return ok

    def request_cancel(self) -> bool:
        """Cancel the pipeline (from any cancellable state)."""
        ok = self.transition("cancel")
        if ok:
            self._cancel_auto_resume_timer()
            self._resume_event.set()  # unblock worker so it can exit
        return ok

    # ── Cooperative pause point (called from worker thread) ───────────────────

    def check_pause_point(self) -> bool:
        """
        Called by the worker thread between segments.

        Blocks if the controller is in PAUSING state (waits for resume or
        cancel).  Returns True if the pipeline should continue, False if it
        has been cancelled and the worker should abort.
        """
        with self._lock:
            status = self.status

        if status == PipelineStatus.CANCELLING:
            return False

        if status == PipelineStatus.PAUSING:
            # Confirm we are now fully paused
            self.transition("paused")
            log.info("PipelineController: pipeline paused — waiting for resume or cancel.")
            # Block until resume_event is set
            self._resume_event.wait()
            # After unblocking, check whether we were cancelled or resumed
            with self._lock:
                post_status = self.status
            if post_status == PipelineStatus.CANCELLING:
                return False
            # Transition RESUMING → RUNNING (must be called outside _lock)
            self.transition("running")

        return True

    # ── Rate-limit tracking (Phase 3c) ────────────────────────────────────────

    def record_429(self) -> None:
        """
        Record a consecutive 429 response.  When the count reaches
        ``RATE_LIMIT_PAUSE_THRESHOLD``, raises ``RateLimitPauseError`` so the
        caller can trigger an auto-pause.
        """
        with self._lock:
            self._consecutive_429s += 1
            count = self._consecutive_429s

        log.warning(
            "PipelineController: 429 received (%d consecutive).", count
        )
        if count >= RATE_LIMIT_PAUSE_THRESHOLD:
            raise RateLimitPauseError(
                f"Received {count} consecutive 429 responses — auto-pausing for "
                f"{RATE_LIMIT_AUTO_RESUME_SECS // 3600}h."
            )

    def reset_429_counter(self) -> None:
        """Reset the consecutive-429 counter after a successful API call."""
        with self._lock:
            self._consecutive_429s = 0

    def trigger_rate_limit_pause(self, resume_secs: int = RATE_LIMIT_AUTO_RESUME_SECS) -> None:
        """
        Transition to PAUSED and schedule an auto-resume after ``resume_secs``.
        Called by the worker when it catches ``RateLimitPauseError``.
        """
        self.request_pause()
        # Immediately confirm paused (no cooperative check needed here)
        self.transition("paused")
        log.info(
            "PipelineController: rate-limit auto-pause — will auto-resume in %ds (%dh).",
            resume_secs, resume_secs // 3600,
        )
        self._cancel_auto_resume_timer()
        timer = threading.Timer(resume_secs, self._auto_resume)
        timer.daemon = True
        timer.start()
        self._auto_resume_timer = timer

    def _auto_resume(self) -> None:
        log.info("PipelineController: auto-resume timer fired.")
        self.request_resume()

    def _cancel_auto_resume_timer(self) -> None:
        if self._auto_resume_timer is not None:
            self._auto_resume_timer.cancel()
            self._auto_resume_timer = None

    # ── Checkpoint persistence ────────────────────────────────────────────────

    @staticmethod
    def _book_hash(book_path: str) -> str:
        """SHA-256 of the first 64 KB of the book file (fast, stable identifier)."""
        h = hashlib.sha256()
        try:
            with open(book_path, "rb") as f:
                h.update(f.read(65536))
        except OSError:
            h.update(book_path.encode())
        return f"sha256:{h.hexdigest()}"

    def _checkpoint_path(self, book_path: str) -> Optional[Path]:
        if self._output_dir is None:
            return None
        book_hash = self._book_hash(book_path)
        # Use last 16 hex chars of hash as filename to keep it short
        short = book_hash.split(":")[-1][:16]
        cp_dir = self._output_dir / CHECKPOINT_SUBDIR
        cp_dir.mkdir(parents=True, exist_ok=True)
        return cp_dir / f"{short}.json"

    def save_checkpoint(
        self,
        book_path: str,
        stage: str,
        completed_segments: List[int],
        extracted_recipes: List[Any],
        toc_entries: List[Any],
    ) -> None:
        """
        Persist current progress to a JSON checkpoint file.

        Raises ``CheckpointError`` if the file cannot be written.
        """
        cp_path = self._checkpoint_path(book_path)
        if cp_path is None:
            return  # no output_dir configured — skip silently

        data: Dict[str, Any] = {
            "version": _CHECKPOINT_VERSION,
            "book_path": str(book_path),
            "book_hash": self._book_hash(book_path),
            "stage": stage,
            "completed_segments": completed_segments,
            "extracted_recipes": extracted_recipes,
            "toc_entries": toc_entries,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            cp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log.debug("Checkpoint saved: %s", cp_path)
        except OSError as exc:
            raise CheckpointError(f"Could not write checkpoint to '{cp_path}': {exc}") from exc

    def load_checkpoint(self, book_path: str) -> Optional[Dict[str, Any]]:
        """
        Load a checkpoint for ``book_path`` if one exists and is valid.

        Returns the checkpoint dict, or None if no checkpoint is found.
        Raises ``CheckpointError`` if the file exists but is malformed.
        """
        cp_path = self._checkpoint_path(book_path)
        if cp_path is None or not cp_path.exists():
            return None

        try:
            data = json.loads(cp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"Could not read checkpoint '{cp_path}': {exc}"
            ) from exc

        if data.get("version") != _CHECKPOINT_VERSION:
            log.warning(
                "Checkpoint version mismatch (got %s, expected %d) — ignoring.",
                data.get("version"), _CHECKPOINT_VERSION,
            )
            return None

        # Verify the checkpoint belongs to the same file
        stored_hash = data.get("book_hash", "")
        current_hash = self._book_hash(book_path)
        if stored_hash != current_hash:
            log.warning(
                "Checkpoint hash mismatch — file may have changed. Ignoring checkpoint."
            )
            return None

        log.info(
            "Checkpoint loaded: stage=%s, %d completed segment(s).",
            data.get("stage"), len(data.get("completed_segments", [])),
        )
        return data

    def delete_checkpoint(self, book_path: str) -> None:
        """Remove the checkpoint file for ``book_path`` (called on successful completion)."""
        cp_path = self._checkpoint_path(book_path)
        if cp_path and cp_path.exists():
            try:
                cp_path.unlink()
                log.debug("Checkpoint deleted: %s", cp_path)
            except OSError as exc:
                log.warning("Could not delete checkpoint '%s': %s", cp_path, exc)
