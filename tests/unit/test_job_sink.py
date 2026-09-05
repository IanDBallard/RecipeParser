"""JobSink bookkeeping (spec 4.2, 4.3, 4.4, 5.1). No network, no FastAPI."""
from __future__ import annotations

from typing import Any, List

from recipeparser.adapters.job_sink import SKIPPED_LIST_CAP, JobSink
from recipeparser.core.models import Chunk, InputType


def _sink(**kwargs: Any) -> JobSink:
    written: List[Any] = []
    sink = JobSink(
        job_id="job-1",
        user_id="user-1",
        category_ids={},
        write=lambda recipe, user_id, category_ids: written.append(recipe),
        **kwargs,
    )
    sink.written = written  # type: ignore[attr-defined]
    return sink


class _Recipe:
    def __init__(self, title: str = "R") -> None:
        self.title = title


def test_each_result_is_written_immediately_and_counted():
    sink = _sink()

    sink.on_result(_Recipe("A"))
    sink.on_result(_Recipe("B"))

    assert [r.title for r in sink.written] == ["A", "B"]
    assert sink.recipe_count == 2
    assert sink.skipped_count == 0


def test_a_failed_write_is_counted_as_a_skip_not_raised():
    def _boom(recipe, user_id, category_ids):
        raise RuntimeError("insert rejected")

    sink = JobSink(job_id="j", user_id="u", category_ids={}, write=_boom)

    sink.on_result(_Recipe("A"))

    assert sink.recipe_count == 0
    assert sink.skipped_count == 1
    assert "insert rejected" in sink.skipped[0]["reason"]
    assert sink.skipped[0]["index"] == -1


def test_a_skipped_chunk_is_named_by_its_label():
    sink = _sink()

    sink.on_skip(Chunk(text="t", input_type=InputType.URL, label="Sticky Toffee Pudding"), "MAX_TOKENS", 3)

    assert sink.skipped == [{"label": "Sticky Toffee Pudding", "index": 3, "reason": "MAX_TOKENS"}]
    assert sink.skipped_count == 1


def test_a_chunk_with_no_label_is_reported_by_index():
    sink = _sink()

    sink.on_skip(Chunk(text="t", input_type=InputType.PDF), "boom", 7)

    assert sink.skipped[0]["label"] is None
    assert sink.skipped[0]["index"] == 7


def test_the_list_is_capped_but_the_count_is_not():
    sink = _sink()

    for i in range(SKIPPED_LIST_CAP + 10):
        sink.on_skip(Chunk(text="t", input_type=InputType.URL), "boom", i)

    assert len(sink.skipped) == SKIPPED_LIST_CAP
    assert sink.skipped_count == SKIPPED_LIST_CAP + 10


def test_progress_is_emitted_once_per_whole_percent():
    sink = _sink()

    for completed in range(1, 828):
        sink.on_progress("PROCESSING", completed, 827)

    assert sink.progress_updates == sorted(set(sink.progress_updates))
    assert len(sink.progress_updates) <= 100
    assert sink.progress_updates[-1] == 100


def test_finalize_omits_progress_on_failure():
    """A job that died at 60% must not claim it never started."""
    sink = _sink()

    payload = sink.finalize_payload(success=False, error_message="reader exploded")

    assert "progress_pct" not in payload
    assert payload["status"] == "error"
    assert payload["error_message"] == "reader exploded"


def test_finalize_reports_a_hundred_and_the_counts_on_success():
    sink = _sink()
    sink.on_result(_Recipe("A"))
    sink.on_skip(Chunk(text="t", input_type=InputType.URL, label="Lost"), "MAX_TOKENS", 0)

    payload = sink.finalize_payload(success=True, error_message=None)

    assert payload["progress_pct"] == 100
    assert payload["recipe_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["skipped"] == [{"label": "Lost", "index": 0, "reason": "MAX_TOKENS"}]
    assert payload["status"] == "done"
