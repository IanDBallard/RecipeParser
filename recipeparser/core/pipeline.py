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

import contextvars
import logging
import mimetypes
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from recipeparser.core.citation import Citation, host_of, resolve_citation
from recipeparser.core.durations import Span, duration_columns
from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType, RestoreFields, SourceMeta
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.core.servings import servings_for_restore
from recipeparser.core.stages.assemble import assemble
from recipeparser.core.stages.embed import embed
from recipeparser.core.stages.extract import extract
from recipeparser.core.stages.refine import refine
from recipeparser.core.stages.tag import TAG_BATCH_SIZE, TagFailure, tag_batch
from recipeparser.core.ports import CategorySource, ImageStore
from recipeparser.gemini import categorize_batch
from recipeparser.models import CayenneRecipe, CayenneRefinement, IngestResponse, Photo

log = logging.getLogger(__name__)

# Maximum number of concurrent Gemini API calls.
MAX_CONCURRENT_API_CALLS: int = 4


class ChunkResult(NamedTuple):
    """What one chunk produced, and the skip reason of each recipe EXTRACT dropped (D2)."""

    results: List[IngestResponse]
    skipped: List[str]


class _ChunkFailed(Exception):
    """A chunk that failed after EXTRACT named its drops, carrying them so they are still reported (D2)."""

    def __init__(self, cause: Exception, skipped: List[str]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.skipped = skipped


def chunk_stages(chunk: Chunk) -> List[str]:
    """The ordered stage names to run for ``chunk`` (the table in ``RecipePipeline._get_stages``)."""
    if chunk.skip_reason is not None:
        return []                        # a skipped line: no stage, no model call (D6)
    if chunk.input_type == InputType.PAPRIKA_CAYENNE:
        if chunk.pre_parsed_embedding is not None:
            return ["ASSEMBLE"]          # $0 — skip all Gemini calls
        return ["EMBED", "ASSEMBLE"]     # Only embed: skip extract, refine and tag
    # URL, PDF, EPUB, IMAGE, PAPRIKA_LEGACY — full pipeline
    return ["EXTRACT", "REFINE", "EMBED", "ASSEMBLE", "TAG"]


def chunk_needs_ai(chunk: Chunk) -> bool:
    """True when the chunk's recipe is made by the model (REFINE runs): the decision behind ``ai_processed``.

    Not ``pre_parsed is None``: a Cayenne chunk without ``pre_parsed`` still takes no
    REFINE, and a non-Cayenne chunk with one still does.
    """
    return "REFINE" in chunk_stages(chunk)


def chunk_calls_ai(chunk: Chunk) -> bool:
    """True when any stage of the chunk calls Gemini: EXTRACT, REFINE, EMBED and TAG do, ASSEMBLE does not.

    The kill switch covers all of these; only a chunk that ``chunk_needs_ai`` (REFINE) counts against the cap.
    """
    return any(stage != "ASSEMBLE" for stage in chunk_stages(chunk))


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
        client: Any,
        controller: PipelineController,
        category_source: CategorySource,
        concurrency: int = MAX_CONCURRENT_API_CALLS,
        rpm: Optional[int] = None,
        image_store: Optional[ImageStore] = None,
        categorize_fn: Optional[Callable[..., Dict[str, List[str]]]] = None,
        picture_source: Optional[Any] = None,
    ) -> None:
        """
        Args:
            client:             An initialised ``google.genai.Client`` instance.
            controller:         A ``PipelineController`` FSM for pause/cancel/checkpoint.
            category_source:    A ``CategorySource`` implementation for taxonomy loading.
            concurrency:        Maximum number of parallel chunk workers.
            rpm:                Optional RPM override for the GlobalRateLimiter.
                                Only honoured on the first instantiation of the singleton.
            image_store:        Optional ImageStore for chunks that carry raw
                                image bytes.  Without one those bytes are
                                dropped, which is what shipped before.
            categorize_fn:      The TAG stage's model call, ``gemini.categorize_batch``
                                by default; a test passes a fake.
            picture_source:     Optional bucket-picture source (``owns(url)`` and
                                ``download(url)``): a Cayenne restore whose export
                                names a picture without carrying its bytes has it
                                copied into the new account.  Without one the
                                address is kept as it is.
        """
        self._client = client
        self._controller = controller
        self._category_source = category_source
        self._cap = max(1, concurrency)
        self._image_store = image_store
        self._categorize_fn = categorize_fn or categorize_batch
        self._picture_source = picture_source
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
        *,
        on_result: Optional[Callable[[IngestResponse, bool], Optional[bool]]] = None,
        on_skip: Optional[Callable[[Chunk, str, int], None]] = None,
        on_tags: Optional[Callable[[IngestResponse], None]] = None,
        on_tag_failed: Optional[Callable[[TagFailure], None]] = None,
    ) -> List[IngestResponse]:
        """
        Process all chunks and return successfully assembled IngestResponse objects.

        Args:
            chunks:      List of Chunk objects produced by a reader.
            on_progress: Optional callback ``(stage, completed, total) -> None``
                         fired after every chunk (success or failure).
            user_id:     The authenticated user's UUID, forwarded to the
                         CategorySource for taxonomy loading.
            on_result:   Optional callback fired once per assembled recipe, in
                         completion order, from this thread.  Lets a caller
                         persist results as they arrive instead of after the
                         whole batch.  The second argument is True when REFINE
                         ran for the recipe's chunk (AI processed it) and False
                         for a Cayenne restore (AI caps D3).  Returning
                         ``False`` (exactly) means the recipe was not written,
                         so it is not queued for TAG; ``None``/``True`` keep it.
            on_skip:     Optional callback ``(chunk, reason, index) -> None``
                         fired for every chunk that produced no result because
                         of a failure.  ``index`` is the chunk's zero-based
                         position in the ``chunks`` list passed to this call —
                         its submission order, not its completion order —
                         because only Paprika chunks carry a ``label``; EPUB
                         and PDF chunks are always ``label=None``, so the
                         index is the only identification a lost book chunk
                         has.  A chunk that simply contained no recipe is not
                         a skip.  A recipe EXTRACT dropped as rewritten is
                         reported the same way, with the reason naming it.
            on_tags:     Optional callback fired once per tagged recipe, from this
                         thread, after TAG has written its ``grid_categories`` and
                         ``categories`` onto it.  The recipe was already handed to
                         ``on_result``, untagged (Fix Roadmap F-246, design D1).
            on_tag_failed: Optional callback fired once per axis whose TAG call
                         failed twice for a batch (design D8).

        Tagging: REFINE no longer categorises.  Each finished recipe that went
        through REFINE is queued; every ``TAG_BATCH_SIZE`` of them are tagged one
        axis per call, and the rest when the run ends, including on cancel, so no
        written recipe is left untagged.  The returned objects carry their tags.

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
        # The tree's shape, for the prompt and for pruning a parent picked beside
        # its child (F-205). Losing it costs only that pruning, not the tags.
        try:
            parents: Dict[str, str] = self._category_source.load_parents(user_id) if user_axes else {}
        except Exception:
            log.exception("RecipePipeline: failed to load category parents — tags will not be pruned.")
            parents = {}

        # Transition FSM to RUNNING.
        self._controller.transition("start")

        def _worker(chunk: Chunk) -> ChunkResult:
            """Process a single chunk inside a thread-pool worker."""
            # A reader may have pulled a photograph out of the archive; store it
            # before ASSEMBLE so the assembled recipe carries a URL rather than
            # raw bytes.
            if chunk.image_bytes and not chunk.image_url and self._image_store is not None:
                chunk.image_url = self._image_store.put(
                    chunk.image_bytes, str(uuid.uuid4()), chunk.image_content_type
                )
            self._restore_bucket_picture(chunk)
            stages = self._get_stages(chunk)
            return self._process_chunk(chunk, stages)

        # (chunk position, position in the chunk, recipe): chunks finish in any order, and a
        # batch is tagged in source order so the same import asks the same prompt (F-246).
        tag_queue: List[Tuple[int, int, IngestResponse]] = []

        def _categorize(rows: List[Dict[str, Any]], axes: Dict[str, List[str]]) -> Dict[str, List[str]]:
            self._limiter.wait_then_record_start()
            return self._categorize_fn(rows, axes, self._client, parents=parents, limiter=self._limiter)

        def _flush_tags() -> None:
            """Tag what is queued (design D1) and hand each recipe on; never fail the run."""
            batch = [recipe for _i, _j, recipe in sorted(tag_queue, key=lambda q: (q[0], q[1]))]
            tag_queue.clear()
            if not batch or not user_axes:
                return
            self._controller.notify_stage_change("CATEGORIZING")
            failures = tag_batch(batch, user_axes, parents, _categorize)
            for recipe in batch:
                if on_tags is not None:
                    try:
                        on_tags(recipe)
                    except Exception:
                        log.exception("RecipePipeline: on_tags callback failed for one recipe.")
            for failure in failures:
                if on_tag_failed is not None:
                    try:
                        on_tag_failed(failure)
                    except Exception:
                        log.exception("RecipePipeline: on_tag_failed callback failed.")

        def _report_skip(chunk: Chunk, reason: str, index: int) -> None:
            """Tell the caller a chunk produced nothing, and never let that itself fail the run."""
            if on_skip is None:
                return
            try:
                on_skip(chunk, reason, index)
            except Exception:
                log.exception("RecipePipeline: on_skip callback failed — continuing.")

        with ThreadPoolExecutor(max_workers=self._cap) as executor:
            # Capture each chunk's submission position here — completion order
            # (via as_completed below) is not submission order, and that
            # position is the only identification a labelless (EPUB/PDF)
            # chunk has.
            future_to_chunk = {
                # Each worker runs in a copy of the caller's context, so its Gemini calls are
                # attributed to the job (usage.py, AI caps D8). One copy per submit: a Context
                # cannot be entered by two threads at once.
                executor.submit(contextvars.copy_context().run, _worker, chunk): (chunk, index)
                for index, chunk in enumerate(chunks)
            }

            for future in as_completed(future_to_chunk):
                chunk, index = future_to_chunk[future]

                # Cooperative pause/cancel check between chunk completions.
                if not self._controller.check_pause_point():
                    log.info("RecipePipeline: cancelled — stopping after %d/%d chunks.", completed, total)
                    # Cancel remaining futures (best-effort).
                    for f in future_to_chunk:
                        f.cancel()
                    break

                try:
                    # No timeout here on purpose. as_completed() only yields
                    # futures that have already finished, so result() never
                    # blocks and a timeout= would be unreachable — it read as a
                    # per-chunk bound this loop does not enforce. A thread
                    # cannot be cancelled anyway: the executor's __exit__ waits
                    # on shutdown regardless. The real bound is per call, in
                    # gemini._call_with_retry's HTTP timeout.
                    outcome = future.result()
                    all_results.extend(outcome.results)
                    # A Cayenne restore carries its own categories and never passes REFINE.
                    needs_tags = "REFINE" in self._get_stages(chunk)
                    for position, result in enumerate(outcome.results):
                        if on_result is not None:
                            try:
                                if on_result(result, needs_tags) is False:
                                    continue  # refused (the beta cap): not written, so nothing to tag
                            except Exception:
                                # Deliberately NOT re-raised, unlike on_progress below. That
                                # callback guards a stale stage label, which is indistinguishable
                                # from a zombie job. This one is a single recipe's write, and
                                # aborting an 827-recipe import because one row failed is worse
                                # than losing that row and saying so — which _report_skip does.
                                log.exception("RecipePipeline: on_result callback failed for one recipe.")
                                _report_skip(chunk, "result callback failed", index)
                                continue  # not written, so nothing to tag
                        if needs_tags:
                            tag_queue.append((index, position, result))
                    if len(tag_queue) >= TAG_BATCH_SIZE:
                        _flush_tags()
                    for reason in outcome.skipped:
                        _report_skip(chunk, reason, index)
                except Exception as exc:
                    if isinstance(exc, _ChunkFailed):
                        for reason in exc.skipped:
                            _report_skip(chunk, reason, index)
                        exc = exc.cause
                    log.error("RecipePipeline: chunk worker raised unexpectedly — skipping. Error: %s", exc)
                    _report_skip(chunk, f"{type(exc).__name__}: {exc}", index)
                finally:
                    completed += 1
                    if on_progress is not None:
                        try:
                            on_progress("PROCESSING", completed, total)
                        except Exception:
                            log.exception("RecipePipeline: on_progress callback failed — re-raising (§11.4).")
                            raise

        # What is left in the queue, after the last chunk or a cancel (design D1).
        _flush_tags()

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
        (The routing itself is ``chunk_stages``, which ``chunk_needs_ai`` shares.)

        Routing table (§4.2 / §7.3 of PIPELINE_REFACTOR.md):
          PAPRIKA_CAYENNE + embedding  → ['ASSEMBLE']
          PAPRIKA_CAYENNE no embedding → ['EMBED', 'ASSEMBLE']
          All other InputTypes         → ['EXTRACT', 'REFINE', 'EMBED', 'ASSEMBLE', 'TAG']
          (TAG is not per chunk: it runs over every TAG_BATCH_SIZE finished recipes, in run())
        """
        return chunk_stages(chunk)

    # ──────────────────────────────────────────────────────────────────────────
    # Per-chunk processing
    # ──────────────────────────────────────────────────────────────────────────

    def _restore_bucket_picture(self, chunk: Chunk) -> None:
        """Make a Cayenne restore's bucket picture its own, or drop the address (R1).

        The export names the old account's object; it is downloaded and stored again unless
        the chunk already carries bytes.  If that fails the recipe goes without a picture:
        the old address dies with the old account, so it is never kept.
        """
        source = self._picture_source
        pr = chunk.pre_parsed
        if source is None or pr is None or chunk.input_type != InputType.PAPRIKA_CAYENNE:
            return
        old_url = getattr(pr, "image_url", None)
        if not old_url or not source.owns(old_url):
            return
        if not chunk.image_url and not chunk.image_bytes and self._image_store is not None:
            try:
                got = source.download(old_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("restore: could not copy the picture %s (%s).", old_url, exc)
                got = None
            if got is not None:
                chunk.image_url = self._image_store.put(got[0], str(uuid.uuid4()), got[1])
        if not chunk.image_url:
            log.warning("restore: the recipe %r goes without its picture.", chunk.label or pr.title)
            pr.image_url = None
            pr.image_source = None

    def _process_chunk(
        self,
        chunk: Chunk,
        stages: List[str],
    ) -> ChunkResult:
        """
        Execute the stage sequence for a single chunk.

        Returns a ChunkResult: one IngestResponse per recipe kept from the
        chunk, and a skip reason per recipe EXTRACT dropped as rewritten.
        Both are empty if the chunk yields no recipes (not an error).

        This method is called from a ThreadPoolExecutor worker thread.
        Exceptions propagate to the caller (run()) which handles them in the
        per-chunk error boundary.
        """
        if chunk.skip_reason is not None:
            return ChunkResult([], [chunk.skip_reason])

        results: List[IngestResponse] = []

        # ── Fast-path: PAPRIKA_CAYENNE with pre_parsed + embedding ────────────
        if stages == ["ASSEMBLE"]:
            if chunk.pre_parsed is None:
                log.warning("_process_chunk: ASSEMBLE-only stage but pre_parsed is None — skipping chunk.")
                return ChunkResult([], [])
            # Use the pre-parsed data directly; re-assemble to get a clean IngestResponse.
            pr = chunk.pre_parsed
            # Use explicit None check — an empty list is a valid (if degenerate) embedding
            # and must not fall through to pr.embedding (which CayenneRecipe lacks).
            embedding = chunk.pre_parsed_embedding if chunk.pre_parsed_embedding is not None else []
            self._controller.notify_stage_change("EMBEDDING")
            result = assemble(
                recipe=_pre_parsed_to_refinement(pr),
                embedding=embedding,
                source_url=chunk.source_url or pr.source_url,
                image_url=chunk.image_url or pr.image_url,
                grid_categories=pr.grid_categories or {},
                prep_time=pr.prep_time,
                cook_time=pr.cook_time,
                meta=chunk.meta or _source_meta_from(pr),
                ingredient_lines=list(getattr(pr, "ingredient_lines", []) or []),
                direction_steps=list(getattr(pr, "direction_steps", []) or []),
                servings_text=None,
                citation=chunk.citation or _citation_from(pr),
                spans=_restore_spans(pr),
                restore=_restore_fields(pr),
            )
            result.attach_photo(_chunk_photo(chunk))
            return ChunkResult([result], [])

        # ── Fast-path: PAPRIKA_CAYENNE without embedding ──────────────────────
        if stages == ["EMBED", "ASSEMBLE"]:
            if chunk.pre_parsed is None:
                log.warning("_process_chunk: EMBED+ASSEMBLE stage but pre_parsed is None — skipping chunk.")
                return ChunkResult([], [])
            pr = chunk.pre_parsed
            self._controller.notify_stage_change("EMBEDDING")
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
                meta=chunk.meta or _source_meta_from(pr),
                ingredient_lines=list(getattr(pr, "ingredient_lines", []) or []),
                direction_steps=list(getattr(pr, "direction_steps", []) or []),
                servings_text=None,
                citation=chunk.citation or _citation_from(pr),
                spans=_restore_spans(pr),
                restore=_restore_fields(pr),
            )
            result.attach_photo(_chunk_photo(chunk))
            return ChunkResult([result], [])

        # ── Full pipeline: EXTRACT → REFINE → CATEGORIZE → EMBED → ASSEMBLE ──
        plain_text = chunk.input_type == InputType.PAPRIKA_LEGACY

        # EXTRACT
        self._controller.notify_stage_change("EXTRACTING")
        # extract() takes its own slot before each Gemini call it makes (up to three: the baker's
        # table, the extraction, the number-guard retry), so none is taken for it here (F-012).
        extraction = extract(
            chunk_text=chunk.text,
            client=self._client,
            plain_text_mode=plain_text,
            limiter=self._limiter,
        )
        skipped = [f"ingredient lines did not match the source: {title}" for title in extraction.rewritten]
        if not extraction.recipes:
            log.info("_process_chunk: no recipes kept from chunk — skipping.")
            return ChunkResult([], skipped)

        # A later stage failing the chunk must not swallow the drops EXTRACT named (D2).
        try:
            for raw in extraction.recipes:
                # REFINE
                self._controller.notify_stage_change("REFINING")
                self._limiter.wait_then_record_start()
                refined = refine(
                    raw=raw,
                    client=self._client,
                    source_host=host_of(chunk.source_url) if chunk.source_url else None,
                    limiter=self._limiter,  # for the retries inside gemini.py (F-109)
                )

                # Tags come later, from the TAG stage over every TAG_BATCH_SIZE finished recipes (F-246).

                # EMBED
                self._controller.notify_stage_change("EMBEDDING")
                self._limiter.wait_then_record_start()
                embedding = embed(recipe=refined, client=self._client)

                # The photo the model picked from the book's markers, stored per
                # recipe (one chunk can hold several); else the chunk's own.
                photo = _book_photo(chunk, getattr(raw, "photo_filename", None))
                image_url = chunk.image_url
                if photo is not None and self._image_store is not None:
                    image_url = (
                        self._image_store.put(photo.data, str(uuid.uuid4()), photo.content_type) or chunk.image_url
                    )
                elif photo is None:
                    photo = _chunk_photo(chunk)

                # ASSEMBLE (no separate DB stage — stays at EMBEDDING per §7.2)
                result = assemble(
                    recipe=refined,
                    embedding=embedding,
                    source_url=chunk.source_url,
                    image_url=image_url,
                    grid_categories={},
                    prep_time=raw.prep_time if hasattr(raw, "prep_time") else None,
                    cook_time=raw.cook_time if hasattr(raw, "cook_time") else None,
                    meta=chunk.meta,
                    ingredient_lines=list(raw.ingredients),
                    direction_steps=list(raw.directions),
                    servings_text=getattr(raw, "servings", None),
                    citation=resolve_citation(
                        chunk.citation,
                        getattr(raw, "stated_source", None),
                        getattr(raw, "byline", None),
                    ),
                    total_time=getattr(raw, "total_time", None),
                    description=getattr(raw, "description", None),
                    nutritional_info=getattr(raw, "nutritional_info", None),
                    notes=getattr(raw, "notes", None),
                )
                result.attach_photo(photo)
                results.append(result)
        except Exception as exc:
            if not skipped:
                raise
            raise _ChunkFailed(exc, skipped) from exc

        return ChunkResult(results, skipped)


# ──────────────────────────────────────────────────────────────────────────────
# Private helpers
# ──────────────────────────────────────────────────────────────────────────────

def _book_photo(chunk: Chunk, photo_filename: Optional[str]) -> Optional[Photo]:
    """The photo the model named, if the chunk really carries it.

    A name the chunk does not carry (the model misread a marker, or invented
    one) is a recipe without a picture, never a failure.
    """
    if not photo_filename:
        return None
    name = os.path.basename(photo_filename.strip())
    data = chunk.images.get(name)
    if data is None:
        log.info("_process_chunk: the model named photo %r, which this chunk does not carry.", name)
        return None
    content_type = mimetypes.guess_type(name)[0] or "image/jpeg"
    return Photo(data=data, content_type=content_type, filename=name)


def _chunk_photo(chunk: Chunk) -> Optional[Photo]:
    """The photo a reader attached to the chunk itself (a Paprika entry's photo_data)."""
    if not chunk.image_bytes:
        return None
    return Photo(data=chunk.image_bytes, content_type=chunk.image_content_type)


def _source_meta_from(pr: "CayenneRecipe | IngestResponse") -> SourceMeta:
    """
    The six metadata fields of a pre-parsed recipe, in the shape ``assemble()`` reads.

    ``_pre_parsed_to_refinement`` is a shim over the four fields ``embed()`` needs and
    carries none of these, so without this the six would arrive from ``_cayenne_meta``
    and then be dropped on the last hop of a restore.
    """
    return SourceMeta(
        prep_time=pr.prep_time,
        cook_time=pr.cook_time,
        source=pr.source,
        notes=pr.notes,
        rating=pr.rating,
        nutritional_info=pr.nutritional_info,
        description=pr.description,
        difficulty=pr.difficulty,
    )


def _citation_from(pr: "CayenneRecipe | IngestResponse") -> Optional[Citation]:
    """
    The four citation columns of a pre-parsed recipe, in the shape ``assemble()`` reads,
    so a Cayenne export restores what a cook filed with Set source rather than nulling it.
    None when the export predates the columns (no kind), which lets the reader's or the
    model's citation apply as for any other chunk.
    """
    kind = getattr(pr, "source_kind", None)
    if not kind:
        return None
    return Citation(
        kind,
        getattr(pr, "source_key", None),
        getattr(pr, "source_title", None),
        getattr(pr, "source_author", None),
    )


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

    The title is kept exactly as given: a Cayenne restore is the cook's own title, and
    title-casing it would undo what they typed.  The measures REFINE detected ride along.
    """
    return CayenneRefinement(
        title=pr.title,
        base_servings=pr.base_servings,
        structured_ingredients=pr.structured_ingredients,
        tokenized_directions=pr.tokenized_directions,
        # A restore skips REFINE, so the tins and the bake would be dropped here. getattr: an archive
        # that predates them has none.
        pans=list(getattr(pr, "pans", None) or []),
        bake_type=getattr(pr, "bake_type", None),
        bake_step=getattr(pr, "bake_step", None),
        source_uom_system_detected=getattr(pr, "source_uom_system_detected", None),
        source_uom_system_evidence=getattr(pr, "source_uom_system_evidence", None),
    )


_SPAN_KEYS = (
    "prep_min_minutes", "prep_max_minutes", "prep_note",
    "cook_min_minutes", "cook_max_minutes", "cook_note",
    "servings_min", "servings_max", "servings_note",
    "base_servings", "servings_source",
)
_SERVINGS_KEYS = ("base_servings", "servings_source")


def _restore_spans(pr: "CayenneRecipe | IngestResponse") -> Dict[str, Any]:
    """
    The span columns of a pre-parsed recipe, as stored.  Re-deriving them from the text
    columns would lose what only the structured columns hold (a servings span and its note).
    An archive that predates the columns carries none of them; for that alone the text is parsed.

    servings_source is kept as exported when the archive carries it (even as null).  An archive
    from before 9.17 has none, so it is derived here without the model (a restore makes no AI
    call): the tin table may replace base_servings; otherwise the exported number stays.
    """
    if not any(k in pr.model_fields_set for k in _SPAN_KEYS if k not in _SERVINGS_KEYS):
        cols = duration_columns(pr.prep_time, pr.cook_time, None, pr.base_servings)
    else:
        cols = {k: getattr(pr, k) for k in _SPAN_KEYS if k != "servings_source"}
    if "servings_source" in pr.model_fields_set:
        cols["servings_source"] = pr.servings_source
        return cols
    sv = servings_for_restore(
        Span(cols["servings_min"], cols["servings_max"], cols["servings_note"]),
        list(getattr(pr, "direction_steps", None) or []),
        list(getattr(pr, "pans", None) or []),
        getattr(pr, "bake_type", None),
        cols["base_servings"],
    )
    cols["base_servings"], cols["servings_source"] = sv.base, sv.source
    return cols


def _restore_fields(pr: "CayenneRecipe | IngestResponse") -> RestoreFields:
    """The favourite, chosen measures, picture origin and stale flag a Cayenne export carried."""
    return RestoreFields(
        is_favourite=bool(pr.is_favourite),
        source_uom_system=pr.source_uom_system,
        image_source=pr.image_source,
        stale=bool(pr.stale),
    )
