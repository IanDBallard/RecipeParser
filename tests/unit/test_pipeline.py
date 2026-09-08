"""
tests/unit/test_pipeline.py — Phase 4 gate tests for RecipePipeline.

All tests use mock stage functions — zero real API calls.
Gate command: pytest tests/unit/test_pipeline.py -v
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType, SourceMeta
from recipeparser.core.pipeline import RecipePipeline
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.core.ports import CategorySource, ImageStore
from recipeparser.models import (
    CayenneRecipe,
    CayenneRefinement,
    IngestResponse,
    StructuredIngredient,
    TokenizedDirection,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FAKE_EMBEDDING = [0.1] * 1536


def _make_ingest_response(title: str = "Test Recipe") -> IngestResponse:
    return IngestResponse(
        title=title,
        prep_time="10 mins",
        cook_time="20 mins",
        base_servings=4,
        source_url=None,
        image_url=None,
        categories=["Italian"],
        grid_categories={"Cuisine": ["Italian"]},
        structured_ingredients=[
            StructuredIngredient(
                id="ing_01",
                amount=1.5,
                unit="cups",
                name="flour",
                fallback_string="1.5 cups flour",
                converted_amount=None,
                converted_unit=None,
                is_ai_converted=False,
            )
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Mix {{ing_01|1.5 cups flour}}.")
        ],
        embedding=FAKE_EMBEDDING,
    )


def _make_refinement(title: str = "Test Recipe") -> CayenneRefinement:
    return CayenneRefinement(
        title=title,
        base_servings=4,
        structured_ingredients=[
            StructuredIngredient(
                id="ing_01",
                amount=1.5,
                unit="cups",
                name="flour",
                fallback_string="1.5 cups flour",
                converted_amount=None,
                converted_unit=None,
                is_ai_converted=False,
            )
        ],
        tokenized_directions=[
            TokenizedDirection(step=1, text="Mix {{ing_01|1.5 cups flour}}.")
        ],
        grid_categories={"Cuisine": ["Italian"]},
    )


class _FakeCategorySource(CategorySource):
    def load_axes(self, user_id: str = "") -> Dict[str, List[str]]:
        return {"Cuisine": ["Italian", "Mexican"]}

    def load_category_ids(self, user_id: str = "") -> Dict[str, str]:
        return {"Italian": "uuid-italian"}


def _make_pipeline(
    controller: Optional[PipelineController] = None,
    image_store: Optional[ImageStore] = None,
) -> RecipePipeline:
    if controller is None:
        controller = PipelineController()
    GlobalRateLimiter().reset()
    return RecipePipeline(
        client=MagicMock(),
        controller=controller,
        category_source=_FakeCategorySource(),
        rpm=9999,  # effectively unlimited for unit tests
        image_store=image_store,
    )


# ---------------------------------------------------------------------------
# Test: _get_stages routing
# ---------------------------------------------------------------------------

FULL_PIPELINE = ["EXTRACT", "REFINE", "CATEGORIZE", "EMBED", "ASSEMBLE"]


class TestGetStages:
    """Unit tests for RecipePipeline._get_stages() routing logic (§4.2)."""

    def setup_method(self):
        self.pipeline = _make_pipeline()

    def test_paprika_cayenne_with_embedding_routes_to_assemble_only(self):
        chunk = Chunk(
            text="",
            input_type=InputType.PAPRIKA_CAYENNE,
            pre_parsed=_make_ingest_response(),
            pre_parsed_embedding=FAKE_EMBEDDING,
        )
        assert self.pipeline._get_stages(chunk) == ["ASSEMBLE"]

    def test_paprika_cayenne_no_embedding_routes_to_embed_assemble(self):
        chunk = Chunk(
            text="",
            input_type=InputType.PAPRIKA_CAYENNE,
            pre_parsed=_make_ingest_response(),
            pre_parsed_embedding=None,
        )
        assert self.pipeline._get_stages(chunk) == ["EMBED", "ASSEMBLE"]

    def test_url_routes_to_full_pipeline(self):
        chunk = Chunk(text="some url text", input_type=InputType.URL)
        assert self.pipeline._get_stages(chunk) == FULL_PIPELINE

    def test_pdf_routes_to_full_pipeline(self):
        chunk = Chunk(text="some pdf text", input_type=InputType.PDF)
        assert self.pipeline._get_stages(chunk) == FULL_PIPELINE

    def test_epub_routes_to_full_pipeline(self):
        chunk = Chunk(text="some epub text", input_type=InputType.EPUB)
        assert self.pipeline._get_stages(chunk) == FULL_PIPELINE

    def test_paprika_legacy_routes_to_full_pipeline(self):
        chunk = Chunk(text="some legacy text", input_type=InputType.PAPRIKA_LEGACY)
        assert self.pipeline._get_stages(chunk) == FULL_PIPELINE


# ---------------------------------------------------------------------------
# Test: run() behaviour
# ---------------------------------------------------------------------------

# Patch targets for all stage functions used inside RecipePipeline._process_chunk
_PATCH_EXTRACT = "recipeparser.core.pipeline.extract"
_PATCH_REFINE = "recipeparser.core.pipeline.refine"
_PATCH_CATEGORIZE = "recipeparser.core.pipeline.categorize"
_PATCH_EMBED = "recipeparser.core.pipeline.embed"
_PATCH_ASSEMBLE = "recipeparser.core.pipeline.assemble"


class TestPipelineRun:
    """Behavioural tests for RecipePipeline.run() — all stage functions mocked."""

    def test_empty_chunks_returns_empty_list(self):
        pipeline = _make_pipeline()
        results = pipeline.run([])
        assert results == []

    def test_skips_failed_chunk_and_continues(self):
        """A chunk that raises must be skipped; the next chunk must still succeed."""
        good_response = _make_ingest_response("Good Recipe")

        # Two PAPRIKA_CAYENNE chunks with embeddings → ASSEMBLE-only path.
        # First chunk: pre_parsed=None so _process_chunk returns [] (no error raised).
        # We need a chunk that actually raises to test the error boundary.
        # Use URL chunks and mock extract() to raise on the first call only.
        chunk_bad = Chunk(text="bad text", input_type=InputType.URL)
        chunk_good = Chunk(
            text="",
            input_type=InputType.PAPRIKA_CAYENNE,
            pre_parsed=good_response,
            pre_parsed_embedding=FAKE_EMBEDDING,
        )

        with patch(_PATCH_EXTRACT, side_effect=RuntimeError("boom")), \
             patch(_PATCH_ASSEMBLE, return_value=good_response):
            pipeline = _make_pipeline()
            results = pipeline.run([chunk_bad, chunk_good])

        # The bad chunk is skipped; the good chunk succeeds.
        assert len(results) == 1
        assert results[0].title == "Good Recipe"

    def test_calls_on_progress_after_each_chunk(self):
        """on_progress must be called once per chunk regardless of success/failure."""
        chunk1 = Chunk(
            text="",
            input_type=InputType.PAPRIKA_CAYENNE,
            pre_parsed=_make_ingest_response("R1"),
            pre_parsed_embedding=FAKE_EMBEDDING,
        )
        chunk2 = Chunk(
            text="",
            input_type=InputType.PAPRIKA_CAYENNE,
            pre_parsed=_make_ingest_response("R2"),
            pre_parsed_embedding=FAKE_EMBEDDING,
        )

        progress_calls: list = []

        def _on_progress(stage: str, completed: int, total: int) -> None:
            progress_calls.append((stage, completed, total))

        with patch(_PATCH_ASSEMBLE, side_effect=[
            _make_ingest_response("R1"),
            _make_ingest_response("R2"),
        ]):
            pipeline = _make_pipeline()
            pipeline.run([chunk1, chunk2], on_progress=_on_progress)

        assert len(progress_calls) == 2
        # total must always be 2
        assert all(total == 2 for _, _, total in progress_calls)
        # completed values must be 1 and 2 (in some order due to threading)
        completed_values = sorted(c for _, c, _ in progress_calls)
        assert completed_values == [1, 2]

    def test_respects_cancel_signal(self):
        """Cancelling the controller mid-run stops processing remaining chunks."""
        controller = PipelineController()

        # Use many ASSEMBLE-only chunks so the test is fast.
        chunks = [
            Chunk(
                text="",
                input_type=InputType.PAPRIKA_CAYENNE,
                pre_parsed=_make_ingest_response(f"Recipe {i}"),
                pre_parsed_embedding=FAKE_EMBEDDING,
            )
            for i in range(10)
        ]

        assemble_call_count = 0

        def _counting_assemble(*args, **kwargs):
            nonlocal assemble_call_count
            assemble_call_count += 1
            # Cancel after the first successful assemble
            if assemble_call_count == 1:
                controller.request_cancel()
            return _make_ingest_response(f"Recipe {assemble_call_count}")

        with patch(_PATCH_ASSEMBLE, side_effect=_counting_assemble):
            pipeline = _make_pipeline(controller=controller)
            results = pipeline.run(chunks)

        # Fewer than all 10 chunks should have been processed
        assert len(results) < 10

    def test_controller_returns_to_idle_after_cancel(self):
        """
        After a cancelled run completes its wind-down, the controller must
        return to IDLE (not remain stuck in CANCELLING).  Regression test for
        the missing (CANCELLING, 'done') → IDLE transition in fsm.py.
        """
        from recipeparser.core.fsm import PipelineStatus

        controller = PipelineController()

        chunks = [
            Chunk(
                text="",
                input_type=InputType.PAPRIKA_CAYENNE,
                pre_parsed=_make_ingest_response(f"Recipe {i}"),
                pre_parsed_embedding=FAKE_EMBEDDING,
            )
            for i in range(5)
        ]

        def _cancel_on_first(*args, **kwargs):
            controller.request_cancel()
            return _make_ingest_response("Recipe 0")

        with patch(_PATCH_ASSEMBLE, side_effect=_cancel_on_first):
            pipeline = _make_pipeline(controller=controller)
            pipeline.run(chunks)

        # The controller must have wound down to IDLE, not be stuck in CANCELLING.
        assert controller.status == PipelineStatus.IDLE, (
            f"Expected IDLE after cancelled run, got {controller.status}"
        )


    def test_a_cancelled_run_leaves_the_flag_set_for_the_terminal_payload(self):
        """End to end through run(): the API reads controller.cancel_requested
        after run() returns, and run() ends by transitioning back to IDLE."""
        from recipeparser.core.fsm import PipelineStatus

        controller = PipelineController()
        chunks = [
            Chunk(
                text="",
                input_type=InputType.PAPRIKA_CAYENNE,
                pre_parsed=_make_ingest_response(f"Recipe {i}"),
                pre_parsed_embedding=FAKE_EMBEDDING,
            )
            for i in range(5)
        ]

        def _cancel_on_first(*args, **kwargs):
            controller.request_cancel()
            return _make_ingest_response("Recipe 0")

        with patch(_PATCH_ASSEMBLE, side_effect=_cancel_on_first):
            pipeline = _make_pipeline(controller=controller)
            pipeline.run(chunks)

        assert controller.status == PipelineStatus.IDLE
        assert controller.cancel_requested is True

    def test_an_uncancelled_run_leaves_the_flag_clear(self):
        controller = PipelineController()
        chunks = [
            Chunk(
                text="",
                input_type=InputType.PAPRIKA_CAYENNE,
                pre_parsed=_make_ingest_response("Recipe 0"),
                pre_parsed_embedding=FAKE_EMBEDDING,
            )
        ]
        with patch(_PATCH_ASSEMBLE, side_effect=lambda *a, **k: _make_ingest_response("Recipe 0")):
            pipeline = _make_pipeline(controller=controller)
            pipeline.run(chunks)

        assert controller.cancel_requested is False

    def test_a_cancelled_run_does_not_itemise_the_chunks_it_never_attempted(self):
        """Spec S4. The chunks abandoned by the break are cancelled futures, not
        skips: they are counted by total_chunks, not named in the skipped list."""
        controller = PipelineController()
        chunks = [
            Chunk(
                text="",
                input_type=InputType.PAPRIKA_CAYENNE,
                pre_parsed=_make_ingest_response(f"Recipe {i}"),
                pre_parsed_embedding=FAKE_EMBEDDING,
            )
            for i in range(10)
        ]
        skips: List[str] = []

        def _cancel_on_first(*args, **kwargs):
            controller.request_cancel()
            return _make_ingest_response("Recipe 0")

        with patch(_PATCH_ASSEMBLE, side_effect=_cancel_on_first):
            pipeline = _make_pipeline(controller=controller)
            results = pipeline.run(
                chunks,
                on_skip=lambda chunk, reason, index: skips.append(reason),
            )

        assert len(results) < 10
        assert skips == []


# ---------------------------------------------------------------------------
# Test: on_result / on_skip streaming callbacks (Task 3)
# ---------------------------------------------------------------------------

def test_on_result_fires_per_recipe_and_on_skip_names_the_failed_chunk():
    """Spec 4.2, 4.3: results stream out as they finish and failures are reported, not swallowed."""
    good = Chunk(text="good", input_type=InputType.URL, label="Good One")
    bad = Chunk(text="bad", input_type=InputType.URL, label="Bad One")
    results_seen: List[IngestResponse] = []
    skips_seen: List[tuple] = []

    pipeline = _make_pipeline()
    with patch.object(
        RecipePipeline,
        "_process_chunk",
        side_effect=lambda chunk, stages, axes: (
            [_make_ingest_response("Good One")] if chunk.text == "good" else _raise(RuntimeError("boom"))
        ),
    ):
        returned = pipeline.run(
            [good, bad],
            on_result=results_seen.append,
            on_skip=lambda chunk, reason, index: skips_seen.append((chunk.label, reason, index)),
        )

    assert [r.title for r in results_seen] == ["Good One"]
    assert [r.title for r in returned] == ["Good One"]
    assert len(skips_seen) == 1
    assert skips_seen[0][0] == "Bad One"
    assert "boom" in skips_seen[0][1]
    # `bad` sits at index 1 of the [good, bad] list passed to run().
    assert skips_seen[0][2] == 1


def test_a_raising_on_result_does_not_abort_the_run():
    """Unlike on_progress: one failed write must not discard the rest of an 827-recipe import."""
    chunks = [Chunk(text=f"c{i}", input_type=InputType.URL) for i in range(3)]

    pipeline = _make_pipeline()
    with patch.object(
        RecipePipeline,
        "_process_chunk",
        side_effect=lambda chunk, stages, axes: [_make_ingest_response(chunk.text)],
    ):
        returned = pipeline.run(chunks, on_result=lambda _r: (_ for _ in ()).throw(RuntimeError("write failed")))

    assert len(returned) == 3


def test_on_skip_reports_submission_position_not_completion_order():
    """
    Ruling R5: the consumer keys a lost chunk by its position in the input
    batch — only Paprika chunks carry a label (Task 2); EPUB/PDF chunks are
    always label=None, so the index is the only identification a book chunk
    has.  Chunks complete via ThreadPoolExecutor + as_completed, which is not
    submission order, so the index must be captured at submission time, not
    read off a counter in the collecting loop.
    """
    chunks = [
        Chunk(text="slow-good-0", input_type=InputType.URL),
        Chunk(text="bad-1", input_type=InputType.URL),
        Chunk(text="slow-good-2", input_type=InputType.URL),
    ]
    skips_seen: List[tuple] = []

    def _side_effect(chunk, stages, axes):
        if chunk.text == "bad-1":
            raise RuntimeError("boom")
        # Slow successes finish after the fast failure, so completion order
        # is [bad-1, slow-good-0, slow-good-2] while submission order is
        # [slow-good-0, bad-1, slow-good-2].
        time.sleep(0.2)
        return [_make_ingest_response(chunk.text)]

    pipeline = _make_pipeline()
    with patch.object(RecipePipeline, "_process_chunk", side_effect=_side_effect):
        pipeline.run(
            chunks,
            on_skip=lambda chunk, reason, index: skips_seen.append((reason, index)),
        )

    assert len(skips_seen) == 1
    # "bad-1" was submitted at index 1 — a counter of completions-so-far or
    # skips-so-far would both wrongly read 0 here, since this is the first
    # (and only) chunk to complete and the only chunk to skip.
    assert skips_seen[0][1] == 1


def _raise(exc: Exception):
    raise exc


# ---------------------------------------------------------------------------
# Test: image_store (Task 4)
# ---------------------------------------------------------------------------


class _FakeImageStore(ImageStore):
    """Records what it was asked to store; returns a predictable URL."""

    def __init__(self, url: Optional[str] = "https://example.test/stored.jpg") -> None:
        self.url = url
        self.calls: List[bytes] = []

    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]:
        self.calls.append(image_bytes)
        return self.url


def _assemble_reflecting_image_url(
    *, recipe, embedding, source_url, image_url, grid_categories, prep_time, cook_time, meta=None
):
    """Stand-in for the real ``assemble`` stage: echoes the image_url it was
    actually called with, so a test can prove the URL travels all the way
    into the returned IngestResponse rather than just landing on the chunk."""
    return _make_ingest_response("R").model_copy(update={"image_url": image_url})


def test_chunk_image_bytes_are_stored_and_the_url_reaches_the_recipe():
    """Spec 4.5: 466 photographs were read and dropped; they must reach the assembled recipe.

    Drives the real ASSEMBLE-only fast path (no ``_process_chunk`` patching) so
    this proves the URL reaches ``IngestResponse.image_url`` via one of the
    real ``image_url=chunk.image_url`` call sites, not merely that it lands on
    the chunk — and stays indifferent to which stage of the pipeline performs
    the upload.
    """
    store = _FakeImageStore()
    chunk = Chunk(
        text="",
        input_type=InputType.PAPRIKA_CAYENNE,
        pre_parsed=_make_ingest_response("R"),
        pre_parsed_embedding=FAKE_EMBEDDING,
        image_bytes=b"\xff\xd8jpegbytes",
    )

    pipeline = _make_pipeline(image_store=store)
    with patch(_PATCH_ASSEMBLE, side_effect=_assemble_reflecting_image_url):
        results = pipeline.run([chunk])

    assert store.calls == [b"\xff\xd8jpegbytes"]
    assert chunk.image_url == "https://example.test/stored.jpg"
    assert len(results) == 1
    assert results[0].image_url == "https://example.test/stored.jpg"


def test_a_failed_upload_still_yields_the_recipe():
    """A missing photograph is not a reason to lose a recipe."""
    store = _FakeImageStore(url=None)
    chunk = Chunk(
        text="",
        input_type=InputType.PAPRIKA_CAYENNE,
        pre_parsed=_make_ingest_response("R"),
        pre_parsed_embedding=FAKE_EMBEDDING,
        image_bytes=b"bytes",
    )

    pipeline = _make_pipeline(image_store=store)
    with patch(_PATCH_ASSEMBLE, side_effect=_assemble_reflecting_image_url):
        results = pipeline.run([chunk])

    assert len(results) == 1
    assert chunk.image_url is None
    assert results[0].image_url is None


_CAPTURED_META: dict = {}


def _assemble_capturing_meta(
    *, recipe, embedding, source_url, image_url, grid_categories, prep_time, cook_time, meta
):
    """Stand-in for assemble() that records the meta it was handed."""
    _CAPTURED_META["meta"] = meta
    return _make_ingest_response("R")


def test_a_legacy_paprika_chunks_meta_reaches_assemble():
    """Design 5.4 - what the reader read survives the whole pipeline."""
    _CAPTURED_META.clear()
    meta = SourceMeta(prep_time="20 min", cook_time="1 hr")
    chunk = Chunk(
        text="Pie\n\nIngredients:\nx\n\nDirections:\ny",
        input_type=InputType.PAPRIKA_LEGACY,
        meta=meta,
    )

    pipeline = _make_pipeline()
    # extract() only has to return one non-empty item: refine is patched over it.
    with patch(_PATCH_EXTRACT, return_value=[_make_refinement("Pie")]), \
         patch(_PATCH_REFINE, return_value=_make_refinement("Pie")), \
         patch(_PATCH_CATEGORIZE, return_value={}), \
         patch(_PATCH_EMBED, return_value=FAKE_EMBEDDING), \
         patch(_PATCH_ASSEMBLE, side_effect=_assemble_capturing_meta):
        pipeline.run([chunk])

    assert _CAPTURED_META["meta"] is meta


def test_flow_b_restores_the_six_metadata_fields():
    """Design 6.3 - a Cayenne archive round-trips the new fields."""
    pre_parsed = CayenneRecipe(
        title="Chicken Pie",
        structured_ingredients=[],
        tokenized_directions=[],
        source="Bon Appetit",
        notes="Chill the dough.",
        rating=4,
        nutritional_info="520 kcal",
        description="A cold-weather pie.",
        difficulty="Moderate",
    )
    chunk = Chunk(
        text="",
        input_type=InputType.PAPRIKA_CAYENNE,
        pre_parsed=pre_parsed,
        pre_parsed_embedding=FAKE_EMBEDDING,
    )

    # The real assemble(), not a stand-in: the point is that the six survive the
    # last hop into the IngestResponse, and a stand-in would prove nothing about it.
    result = _make_pipeline().run([chunk])[0]

    assert result.source == "Bon Appetit"
    assert result.notes == "Chill the dough."
    assert result.rating == 4
    assert result.nutritional_info == "520 kcal"
    assert result.description == "A cold-weather pie."
    assert result.difficulty == "Moderate"


def test_a_worker_timeouterror_is_reported_accurately_not_as_a_segment_timeout():
    """A worker that raises TimeoutError must be reported for what it is.

    The executor loop used to wrap ``future.result(timeout=SEGMENT_TIMEOUT_SECS)``
    in ``except TimeoutError``, which could never fire from the timeout itself:
    ``as_completed()`` only yields futures that have already finished, so
    ``result()`` never blocks. The handler did catch a TimeoutError raised by
    the worker's own code, and reported it as "timed out after 300s" — naming a
    per-chunk bound the pipeline never actually enforced. The skip reason should
    say what really happened.
    """
    skips = []
    chunk = Chunk(text="some text", input_type=InputType.URL)

    with patch(_PATCH_EXTRACT, side_effect=TimeoutError("upstream read timed out")):
        pipeline = _make_pipeline()
        pipeline.run([chunk], on_skip=lambda *args: skips.append(args))

    assert len(skips) == 1, f"expected exactly one skip, got {skips}"
    reason = " ".join(str(a) for a in skips[0])
    assert "TimeoutError" in reason, f"skip reason should name the real error: {reason!r}"
    assert "upstream read timed out" in reason, f"skip reason should carry the message: {reason!r}"
    assert "300" not in reason, (
        f"skip reason invents a 300s per-chunk bound the pipeline never enforced: {reason!r}"
    )
