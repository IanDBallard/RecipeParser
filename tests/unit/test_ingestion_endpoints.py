"""Both endpoints must hand the pipeline a sink (spec 4.3, 4.4). No live writes."""
from __future__ import annotations

from unittest.mock import MagicMock

import recipeparser.adapters.api as api


def test_finalize_takes_a_payload(monkeypatch):
    """The sink builds the row; finalize only sends it."""
    sent = {}

    class _Table:
        def update(self, payload):
            sent.update(payload)
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            return None

    client = MagicMock()
    client.table.return_value = _Table()
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: client)
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)

    api._finalize_ingestion_job("job-1", {"status": "done", "recipe_count": 3, "skipped_count": 0})

    assert sent["status"] == "done"
    assert sent["recipe_count"] == 3


def test_the_pipeline_is_not_given_a_batch_writer():
    """Regression: a batch `writer.write(results)` call after the run is what
    made nothing get written until the whole job finished.

    This used to also grep the source for "on_result=" / "on_skip=" /
    "on_progress=" as a stand-in for proving those callbacks are live, but a
    source-text grep is true even for `on_result=None` — exactly the
    regression it claimed to guard. `tests/test_api.py`'s
    `_assert_pipeline_run_wired_to_a_live_sink` (which reaches for
    `on_result.__self__`) and `test_the_patched_write_receives_the_recipe_end_to_end`
    (which proves a real recipe reaches the patched write) carry that coverage
    instead. The absence check below is kept — a source grep is a reasonable
    way to check for the absence of something.
    """
    import inspect

    for endpoint in (api.submit_job, api.submit_file_job):
        source = inspect.getsource(endpoint)
        assert "writer.write(results)" not in source


def test_a_broken_progress_writer_does_not_kill_the_job(monkeypatch):
    """RecipePipeline.run's on_progress re-raises, so a missed percentage must
    stay a lagging bar, not a dead import — building the Supabase client
    (which can itself raise) has to happen inside the try, not before it.
    """
    def _boom():
        raise RuntimeError("supabase client construction failed")

    monkeypatch.setattr(api, "_get_supabase_service_client", _boom)
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)

    write_progress = api._make_progress_writer("job-1")
    write_progress(42)  # must not raise


def test_total_chunks_is_written_to_the_job_row(monkeypatch):
    """The row is inserted before the source is read, so the count arrives as
    an UPDATE once the reader has returned."""
    sent = {}
    matched = {}

    class _Table:
        def update(self, payload):
            sent.update(payload)
            return self

        def eq(self, column, value):
            matched[column] = value
            return self

        def execute(self):
            return None

    client = MagicMock()
    client.table.return_value = _Table()
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: client)
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)

    api._update_total_chunks("job-1", 827)

    assert sent["total_chunks"] == 827
    assert matched["id"] == "job-1"


def test_a_failed_total_chunks_write_does_not_raise(monkeypatch):
    """Third deliberate exception to fail-loud: a missing denominator is a
    notice that omits "of 827", not a job whose state is unknowable. Building
    the client is inside the try because create_client can itself raise -- and
    because a deploy that ran before Cayenne migration 011 must cost one number,
    not the import.
    """
    def _boom():
        raise RuntimeError("supabase client construction failed")

    monkeypatch.setattr(api, "_get_supabase_service_client", _boom)
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)

    api._update_total_chunks("job-1", 5)  # must not raise


def test_total_chunks_is_not_written_during_a_test_run(monkeypatch):
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: True)
    called = []
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: called.append(1))

    api._update_total_chunks("job-1", 5)

    assert called == []


def _capturing_client(sent: dict):
    class _Table:
        def update(self, payload):
            sent.update(payload)
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            return None

    client = MagicMock()
    client.table.return_value = _Table()
    return client


def test_total_chunks_update_carries_the_source_hint_when_known(monkeypatch):
    sent = {}
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: _capturing_client(sent))
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)
    api._update_total_chunks("job-1", 12, source_hint="the best of jane grigson")
    assert sent["total_chunks"] == 12
    assert sent["source_hint"] == "the best of jane grigson"


def test_total_chunks_update_leaves_the_hint_alone_when_unknown(monkeypatch):
    sent = {}
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: _capturing_client(sent))
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)
    api._update_total_chunks("job-1", 1)
    assert sent["total_chunks"] == 1
    assert "source_hint" not in sent


def test_source_key_of_takes_the_majority_key_and_ignores_uncited_chunks():
    from recipeparser.core.citation import book_citation, web_citation
    from recipeparser.core.models import Chunk, InputType

    book = book_citation("Italian Food", "Elizabeth David")
    chunks = [
        Chunk(text="a", input_type=InputType.EPUB, citation=book),
        Chunk(text="b", input_type=InputType.EPUB, citation=book),
        Chunk(text="c", input_type=InputType.URL, citation=web_citation("https://x.test/r")),
        Chunk(text="d", input_type=InputType.IMAGE),
    ]
    assert api._source_key_of(chunks) == "italian food"
    assert api._source_key_of([Chunk(text="d", input_type=InputType.IMAGE)]) is None
    assert api._source_key_of([]) is None
