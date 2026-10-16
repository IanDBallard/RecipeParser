"""The chunk workers see the caller's usage context, and on_result says whether AI made the recipe."""
from __future__ import annotations

from typing import Dict, List
from unittest.mock import MagicMock

from recipeparser import usage
from recipeparser.core.fsm import PipelineController
from recipeparser.core.models import Chunk, InputType
from recipeparser.core.pipeline import ChunkResult, RecipePipeline
from recipeparser.core.ports import CategorySource
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.models import IngestResponse


class _Source(CategorySource):
    def load_axes(self, user_id: str = "") -> Dict[str, List[str]]:
        return {}

    def load_category_ids(self, user_id: str = "") -> Dict[str, str]:
        return {}


def _pipeline(monkeypatch, stages, process_chunk) -> RecipePipeline:
    GlobalRateLimiter().reset()
    p = RecipePipeline(
        client=MagicMock(),
        controller=PipelineController(),
        category_source=_Source(),
        rpm=9999,
        categorize_fn=lambda rows, axes, client, parents=None, limiter=None: {},
    )
    monkeypatch.setattr(p, "_get_stages", lambda chunk: stages)
    monkeypatch.setattr(p, "_process_chunk", process_chunk)
    return p


def _recipe(title: str) -> IngestResponse:
    return IngestResponse(
        title=title, prep_time=None, cook_time=None, base_servings=4, source_url=None,
        image_url=None, categories=[], grid_categories={}, structured_ingredients=[],
        tokenized_directions=[], embedding=[0.1] * 1536,
    )


def test_worker_threads_see_the_callers_context(monkeypatch) -> None:
    seen = []

    def fake_process_chunk(chunk, stages):
        seen.append(usage.ai_context.get())
        return ChunkResult([], [])

    p = _pipeline(monkeypatch, ["EXTRACT", "REFINE"], fake_process_chunk)
    chunks = [Chunk(text=f"c{i}", input_type=InputType.URL) for i in range(5)]
    with usage.attributed("u1", "import", "j1", get_client=lambda: None) as ctx:
        p.run(chunks, user_id="u1")
    assert len(seen) == 5
    assert all(s is ctx for s in seen)


def test_on_result_says_whether_ai_made_the_recipe(monkeypatch) -> None:
    def fake_process_chunk(chunk, stages):
        return ChunkResult([_recipe("A"), _recipe("B")], [])

    chunk = Chunk(text="x", input_type=InputType.URL)

    got = []
    p = _pipeline(monkeypatch, ["ASSEMBLE"], fake_process_chunk)
    p.run([chunk], on_result=lambda r, ai: got.append((r.title, ai)))
    assert got == [("A", False), ("B", False)]

    got = []
    p = _pipeline(monkeypatch, ["EXTRACT", "REFINE"], fake_process_chunk)
    p.run([chunk], on_result=lambda r, ai: got.append((r.title, ai)))
    assert got == [("A", True), ("B", True)]
