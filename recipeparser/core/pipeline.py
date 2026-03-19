"""
recipeparser/core/pipeline.py — RecipePipeline orchestrator.

Phase 4 of the PIPELINE_REFACTOR.  Replaces the monolithic ``process_epub()``
function with a clean, testable orchestrator that:

  - Routes each Chunk through the correct stage sequence based on InputType
  - Enforces per-chunk error boundaries (one bad chunk never aborts the batch)
  - Calls on_progress after every chunk (success or failure)
  - Respects pause/cancel signals from PipelineController
  - Acquires GlobalRateLimiter slots before every Gemini API call
  - Processes chunks in parallel via ThreadPoolExecutor

TID rule: this module lives in ``core/`` and therefore MUST NOT import from
``recipeparser.io`` or ``recipeparser.adapters``.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.core.stages.assemble import assemble
from recipeparser.core.stages.categorize import categorize
from recipeparser.core.stages.embed import embed
from recipeparser.core.stages.extract import extract
from recipeparser.core.stages.refine import refine
from recipeparser.core.ports import CategorySource
from recipeparser.models import CayenneRecipe, IngestResponse

log = logging.getLogger(__name__)

# Maximum number of concurrent Gemini API calls.
MAX_CONCURRENT_API_CALLS: int = 4

# Per-chunk timeout in seconds (prevents a single hung chunk from blocking forever).
SEGMENT_TIMEOUT_SECS: int = 300


class RecipePipeline:
    """
    Orchestrator that processes a list of Chunks through the appropriate
    stage sequence and returns a list of IngestResponse objects.

    Stage routing (§4.2 of PIPELINE_REFACTOR.md):
      - PAPRIKA_CAYENNE + embedding present  → ['ASSEMBLE']          ($0)
      - PAPRIKA_CAYENNE + no embedding       → ['EMBED', 'ASSEMBLE'] (1 call)
      - All other types                      → full pipeline          (3 calls)

    Error handling:
      Each chunk is processed inside an isolated try/except.  A failed chunk
      is logged and skipped — it never aborts the batch.  on_progress is
      called after every chunk regardless of success or failure.

    Thread safety:
      Chunks are processed in parallel via ThreadPoolExecutor.  The
      GlobalRateLimiter singleton serialises Gemini API slots across all
      concurrent workers and pipeline instances.
    """

    def __init__(
        self,
        client,
        controller: PipelineController,
        category_source: CategorySource,
        uom_system: str = "US",
        measure_preference: str = "Volume",
        concurrency: int = MAX_CONCURRENT_API_CALLS,
        rpm: Optional[int] = None,
    ) -> None:
        """
        Args:
            client:             An initialised ``google.genai.Client`` instance.
            controller:         A ``PipelineController`` FSM for pause/cancel/checkpoint.
            category_source:    A ``CategorySource`` implementation for taxonomy loading.
            uom_system:         User's preferred unit system ("US" | "Metric" | "Imperial").
            measure_preference: User's preferred measure type ("Volume" | "Weight").
            concurrency:        Maximum number of parallel chunk workers.
            rpm:                Optional RPM override for the GlobalRateLimiter.
                                Only honoured on the first instantiation of the singleton.
        """
        self._client = client
        self._controller = controller
        self._category_source = category_source
        self._uom_system = uom_system
        self._measure_preference = measure_preference
        self._cap = max(1, concurrency)
        # Initialise (or retrieve) the process-level rate limiter.
        if rpm is not None:
            self._limiter = GlobalRateLimiter(rpm=rpm)
        else:
            self._limiter = GlobalRateLimiter()

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def run(
        self,
        chunks: List[Chunk],
        on_progress: Optional[Callable[[str, int, int], None]] = None,
        user_id: str = "",
    ) -> List[IngestResponse]:
        """
        Process all chunks and return successfully assembled IngestResponse objects.

        Args:
            chunks:      List of Chunk objects produced by a reader.
            on_progress: Optional callback ``(stage, completed, total) -> None``
                         fired after every chunk (success or failure).
            user_id:     The authenticated user's UUID, forwarded to the
                         CategorySource for taxonomy loading.

        Returns:
            All successfully processed IngestResponse objects.  Chunks that
            fail are logged and excluded from the result — they do not raise.
        """
        if not chunks:
            log.info("RecipePipeline.run(): no chunks to process.")
            return []

        total = len(chunks)
        completed = 0
        all_results: List[IngestResponse] = []

        # Pre-load taxonomy axes once for the whole batch (avoids N round-trips).
        try:
            user_axes: Dict[str, List[str]] = self._category_source.load_axes(user_id)
        except Exception:
            log.exception("RecipePipeline: failed to load category axes — proceeding without categorisation.")
            user_axes = {}

        # Transition FSM to RUNNING.
        self._controller.transition("start")

        def _worker(chunk: Chunk) -> List[IngestResponse]:
            """Process a single chunk inside a thread-pool worker."""
            stages = self._get_stages(chunk)
            return self._process_chunk(chunk, stages, user_axes)

        with ThreadPoolExecutor(max_workers=self._cap) as executor:
            future_to_chunk = {
                executor.submit(_worker, chunk): chunk
                for chunk in chunks
            }

            for future in as_completed(future_to_chunk):
                # Cooperative pause/cancel check between chunk completions.
                if not self._controller.check_pause_point():
                    log.info("RecipePipeline: cancelled — stopping after %d/%d chunks.", completed, total)
                    # Cancel remaining futures (best-effort).
                    for f in future_to_chunk:
                        f.cancel()
                    break

                try:
                    results = future.result(timeout=SEGMENT_TIMEOUT_SECS)
                    all_results.extend(results)
                except TimeoutError:
                    log.warning("RecipePipeline: chunk timed out after %ds — skipping.", SEGMENT_TIMEOUT_SECS)
                except Exception as exc:
                    log.error("RecipePipeline: chunk worker raised unexpectedly — skipping. Error: %s", exc)
                finally:
                    completed += 1
                    if on_progress is not None:
                        try:
                            on_progress("PROCESSING", completed, total)
                        except Exception:
                            log.exception("RecipePipeline: on_progress callback failed — re-raising (§11.4).")
                            raise

        # Transition FSM back to IDLE on normal completion.
        self._controller.transition("done")

        log.info(
            "RecipePipeline.run(): finished. %d/%d chunks succeeded → %d recipe(s).",
            completed, total, len(all_results),
        )
        return all_results

    # ──────────────────────────────────────────────────────────────────────────
    # Stage routing
    # ──────────────────────────────────────────────────────────────────────────

    def _get_stages(self, chunk: Chunk) -> List[str]:
        """
        Return the ordered list of stage names to execute for this chunk.

        Routing table (§4.2 / §7.3 of PIPELINE_REFACTOR.md):
          PAPRIKA_CAYENNE + embedding  → ['ASSEMBLE']
          PAPRIKA_CAYENNE no embedding → ['EMBED', 'ASSEMBLE']
          All other InputTypes         → ['EXTRACT', 'REFINE', 'CATEGORIZE', 'EMBED', 'ASSEMBLE']
        """
        if chunk.input_type == InputType.PAPRIKA_CAYENNE:
            if chunk.pre_parsed_embedding is not None:
                return ["ASSEMBLE"]          # $0 — skip all Gemini calls
            return ["EMBED", "ASSEMBLE"]     # Only embed, skip extract/refine/categorize
        # URL, PDF, EPUB, PAPRIKA_LEGACY — full pipeline
        return ["EXTRACT", "REFINE", "CATEGORIZE", "EMBED", "ASSEMBLE"]

    # ──────────────────────────────────────────────────────────────────────────
    # Per-chunk processing
    # ──────────────────────────────────────────────────────────────────────────

    def _process_chunk(
        self,
        chunk: Chunk,
        stages: List[str],
        user_axes: Dict[str, List[str]],
    ) -> List[IngestResponse]:
        """
        Execute the stage sequence for a single chunk.

        Returns a list of IngestResponse objects (one per recipe found in the
        chunk).  Returns [] if the chunk yields no recipes (not an error).

        This method is called from a ThreadPoolExecutor worker thread.
        Exceptions propagate to the caller (run()) which handles them in the
        per-chunk error boundary.
        """
        results: List[IngestResponse] = []

        # ── Fast-path: PAPRIKA_CAYENNE with pre_parsed + embedding ────────────
        if stages == ["ASSEMBLE"]:
            if chunk.pre_parsed is None:
                log.warning("_process_chunk: ASSEMBLE-only stage but pre_parsed is None — skipping chunk.")
                return []
            # Use the pre-parsed data directly; re-assemble to get a clean IngestResponse.
            pr = chunk.pre_parsed
            # Use explicit None check — an empty list is a valid (if degenerate) embedding
            # and must not fall through to pr.embedding (which CayenneRecipe lacks).
            embedding = chunk.pre_parsed_embedding if chunk.pre_parsed_embedding is not None else []
            result = assemble(
                recipe=_pre_parsed_to_refinement(pr),
                embedding=embedding,
                source_url=chunk.source_url or pr.source_url,
                image_url=chunk.image_url or pr.image_url,
                grid_categories=pr.grid_categories or {},
                prep_time=pr.prep_time,
                cook_time=pr.cook_time,
            )
            return [result]

        # ── Fast-path: PAPRIKA_CAYENNE without embedding ──────────────────────
        if stages == ["EMBED", "ASSEMBLE"]:
            if chunk.pre_parsed is None:
                log.warning("_process_chunk: EMBED+ASSEMBLE stage but pre_parsed is None — skipping chunk.")
                return []
            pr = chunk.pre_parsed
            self._limiter.wait_then_record_start()
            embedding = embed(
                recipe=_pre_parsed_to_refinement(pr),
                client=self._client,
            )
            result = assemble(
                recipe=_pre_parsed_to_refinement(pr),
                embedding=embedding,
                source_url=chunk.source_url or pr.source_url,
                image_url=chunk.image_url or pr.image_url,
                grid_categories=pr.grid_categories or {},
                prep_time=pr.prep_time,
                cook_time=pr.cook_time,
            )
            return [result]

        # ── Full pipeline: EXTRACT → REFINE → CATEGORIZE → EMBED → ASSEMBLE ──
        plain_text = chunk.input_type == InputType.PAPRIKA_LEGACY

        # EXTRACT
        self._limiter.wait_then_record_start()
        extractions = extract(
            chunk_text=chunk.text,
            client=self._client,
            units=_uom_to_units_key(self._uom_system),
            plain_text_mode=plain_text,
        )
        if not extractions:
            log.info("_process_chunk: no recipes found in chunk — skipping.")
            return []

        for raw in extractions:
            # REFINE
            self._limiter.wait_then_record_start()
            refined = refine(
                raw=raw,
                client=self._client,
                uom_system=self._uom_system,
                measure_preference=self._measure_preference,
            )

            # CATEGORIZE (result is already embedded in refined via refine())
            grid_cats = categorize(
                recipe=refined,
                client=self._client,
                user_axes=user_axes,
            )

            # EMBED
            self._limiter.wait_then_record_start()
            embedding = embed(recipe=refined, client=self._client)

            # ASSEMBLE
            result = assemble(
                recipe=refined,
                embedding=embedding,
                source_url=chunk.source_url,
                image_url=chunk.image_url,
                grid_categories=grid_cats,
                prep_time=raw.prep_time if hasattr(raw, "prep_time") else None,
                cook_time=raw.cook_time if hasattr(raw, "cook_time") else None,
            )
            results.append(result)

        return results


# ──────────────────────────────────────────────────────────────────────────────
# Private helpers
# ──────────────────────────────────────────────────────────────────────────────

def _uom_to_units_key(uom_system: str) -> str:
    """Map user-facing UOM system name to the extract() ``units`` key."""
    mapping = {
        "US": "us",
        "Metric": "metric",
        "Imperial": "imperial",
    }
    return mapping.get(uom_system, "book")


def _pre_parsed_to_refinement(
    pr: "CayenneRecipe | IngestResponse",
) -> "CayenneRefinement":
    """
    Shim: convert a pre-parsed object (either a ``CayenneRecipe`` stored by
    ``PaprikaReader`` or a legacy ``IngestResponse`` from ``_cayenne_meta``)
    into the ``CayenneRefinement`` shape expected by ``assemble()`` and
    ``embed()``.

    Both ``CayenneRecipe`` and ``IngestResponse`` expose the four fields used
    here (``title``, ``base_servings``, ``structured_ingredients``,
    ``tokenized_directions``), so the shim works for either type.
    """
    from recipeparser.models import CayenneRefinement  # local import to avoid circulars

    return CayenneRefinement(
        title=pr.title,
        base_servings=pr.base_servings,
        structured_ingredients=pr.structured_ingredients,
        tokenized_directions=pr.tokenized_directions,
    )
