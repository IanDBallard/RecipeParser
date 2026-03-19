"""
tests/unit/test_pipeline.py — Phase 4 gate tests for RecipePipeline.

All tests use mock stage functions — zero real API calls.
Gate command: pytest tests/unit/test_pipeline.py -v
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType
from recipeparser.core.pipeline import RecipePipeline
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.core.ports import CategorySource
from recipeparser.models import (
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


def _make_pipeline(controller: Optional[PipelineController] = None) -> RecipePipeline:
    if controller is None:
        controller = PipelineController()
    GlobalRateLimiter().reset()
    return RecipePipeline(
        client=MagicMock(),
        controller=controller,
        category_source=_FakeCategorySource(),
        rpm=9999,  # effectively unlimited for unit tests
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
