"""JobSink bookkeeping (spec 4.2, 4.3, 4.4, 5.1). No network, no FastAPI."""
from __future__ import annotations

from typing import Any, Dict, List

from recipeparser.adapters.job_sink import SKIPPED_LIST_CAP, JobSink
from recipeparser.core.models import Chunk, InputType


def _sink(**kwargs: Any) -> JobSink:
    written: List[Any] = []
    # Real arity — write_recipe_to_supabase(recipe, user_id, recipe_id=None,
    # category_ids=None) — not a three-positional shape. A fake shaped like
    # the real function is what makes the keyword-vs-positional defect (see
    # test_write_receives_category_ids_by_keyword_and_recipe_id_stays_none)
    # visible to the suite instead of silently binding by position.
    sink = JobSink(
        job_id="job-1",
        user_id="user-1",
        category_ids={},
        write=lambda recipe, user_id, recipe_id=None, category_ids=None: written.append(recipe),
        now=kwargs.pop("now", lambda: "fixed-ts"),
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
    def _boom(recipe, user_id, recipe_id=None, category_ids=None):
        raise RuntimeError("insert rejected")

    sink = JobSink(job_id="j", user_id="u", category_ids={}, write=_boom, now=lambda: "fixed-ts")

    sink.on_result(_Recipe("A"))

    assert sink.recipe_count == 0
    assert sink.skipped_count == 1
    assert "insert rejected" in sink.skipped[0]["reason"]
    assert sink.skipped[0]["index"] == -1


def test_write_receives_category_ids_by_keyword_and_recipe_id_stays_none():
    """The keyword-vs-positional defect: write_recipe_to_supabase's third
    parameter is recipe_id, fourth is category_ids. Three positional args
    would file the category map as the row id. A fake with the real
    function's arity — not a same-named-third-parameter stand-in — is what
    makes that failure visible instead of silently binding by position.
    """
    calls: List[Dict[str, Any]] = []

    def _write(recipe, user_id, recipe_id=None, category_ids=None):
        calls.append({"recipe_id": recipe_id, "category_ids": category_ids})

    sink = JobSink(
        job_id="job-1",
        user_id="user-1",
        category_ids={"Dessert": "cat-uuid-1"},
        write=_write,
        now=lambda: "fixed-ts",
    )

    sink.on_result(_Recipe("A"))

    assert calls[0]["category_ids"] == {"Dessert": "cat-uuid-1"}
    assert calls[0]["recipe_id"] is None


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
    assert payload["updated_at"] == "fixed-ts"


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
    assert payload["updated_at"] == "fixed-ts"


def test_a_cancelled_run_finalizes_as_cancelled_keeping_what_it_wrote():
    """The recipes a cancelled import already wrote are really there, so the
    count is real; the percentage is whatever the last update wrote, because a
    job stopped at 60% neither reached 100 nor never started."""
    sink = _sink()
    sink.on_result(_Recipe("A"))
    sink.on_result(_Recipe("B"))

    payload = sink.finalize_payload(success=True, cancelled=True)

    assert payload["status"] == "cancelled"
    assert payload["recipe_count"] == 2
    assert "progress_pct" not in payload


def test_a_cancelled_run_keeps_stage_done():
    """stage says how far the pipeline got; status says how it ended. A
    'CANCELLED' stage would break the client's IngestionStage union."""
    sink = _sink()

    payload = sink.finalize_payload(success=True, cancelled=True)

    assert payload["stage"] == "DONE"


def test_a_run_that_raised_is_an_error_even_if_cancel_was_requested():
    """A cancel request racing an exception must not relabel the failure."""
    sink = _sink()

    payload = sink.finalize_payload(success=False, error_message="reader exploded", cancelled=True)

    assert payload["status"] == "error"
    assert payload["stage"] == "ERROR"
    assert payload["error_message"] == "reader exploded"


def test_an_uncancelled_success_is_unchanged():
    sink = _sink()
    sink.on_result(_Recipe("A"))

    payload = sink.finalize_payload(success=True)

    assert payload["status"] == "done"
    assert payload["stage"] == "DONE"
    assert payload["progress_pct"] == 100


def test_a_cancelled_run_reports_only_the_chunks_it_actually_attempted():
    """Spec S4: un-attempted chunks are counted by the denominator, not
    itemised. 'You stopped it' is one fact, not seven hundred rows -- and the
    skipped list is for losses worth recovering."""
    sink = _sink()
    sink.on_result(_Recipe("A"))
    sink.on_skip(Chunk(text="t", input_type=InputType.URL, label="Lost"), "MAX_TOKENS", 1)

    payload = sink.finalize_payload(success=True, cancelled=True)

    assert payload["skipped_count"] == 1
    assert payload["skipped"] == [{"label": "Lost", "index": 1, "reason": "MAX_TOKENS"}]
