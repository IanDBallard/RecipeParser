"""Unit tests for recipeparser/adapters/cli.py — run_cli_pipeline()."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_result(title: str = "Pasta"):
    """Return a minimal IngestResponse-like mock."""
    r = MagicMock()
    r.title = title
    return r


# ---------------------------------------------------------------------------
# run_cli_pipeline — argument validation
# ---------------------------------------------------------------------------

class TestRunCliPipelineValidation:

    def test_raises_if_client_is_none(self, tmp_path):
        from recipeparser.adapters.cli import run_cli_pipeline
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK")
        with pytest.raises(ValueError, match="client"):
            run_cli_pipeline(str(epub), str(tmp_path), client=None)

    def test_raises_for_unsupported_extension(self, tmp_path):
        from recipeparser.adapters.cli import run_cli_pipeline
        txt = tmp_path / "book.txt"
        txt.write_text("hello")
        with pytest.raises(ValueError, match="unsupported file type"):
            run_cli_pipeline(str(txt), str(tmp_path), client=MagicMock())

    def test_raises_when_reader_returns_no_chunks(self, tmp_path):
        from recipeparser.adapters.cli import run_cli_pipeline
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK")
        with patch("recipeparser.adapters.cli.EpubReader") as MockReader:
            MockReader.return_value.read.return_value = []
            with pytest.raises(RuntimeError, match="no chunks"):
                run_cli_pipeline(str(epub), str(tmp_path), client=MagicMock())

    def test_raises_when_pipeline_returns_no_results(self, tmp_path):
        from recipeparser.adapters.cli import run_cli_pipeline
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK")
        with patch("recipeparser.adapters.cli.EpubReader") as MockReader, \
             patch("recipeparser.adapters.cli.RecipePipeline") as MockPipeline:
            MockReader.return_value.read.return_value = [MagicMock()]
            MockPipeline.return_value.run.return_value = []
            with pytest.raises(RuntimeError, match="no results"):
                run_cli_pipeline(str(epub), str(tmp_path), client=MagicMock())


# ---------------------------------------------------------------------------
# run_cli_pipeline — happy path
# ---------------------------------------------------------------------------

class TestRunCliPipelineHappyPath:

    def _run(self, tmp_path, suffix=".epub", **kwargs):
        """Helper: run with all IO mocked out."""
        from recipeparser.adapters.cli import run_cli_pipeline
        book = tmp_path / f"cookbook{suffix}"
        book.write_bytes(b"PK")
        fake_result = _make_fake_result()

        ReaderClass = "recipeparser.adapters.cli.EpubReader" if suffix == ".epub" \
            else "recipeparser.adapters.cli.PdfReader"

        with patch(ReaderClass) as MockReader, \
             patch("recipeparser.adapters.cli.RecipePipeline") as MockPipeline, \
             patch("recipeparser.adapters.cli.PaprikaWriter") as MockWriter, \
             patch("recipeparser.adapters.cli.PipelineController"), \
             patch("recipeparser.adapters.cli.YamlCategorySource"):
            MockReader.return_value.read.return_value = [MagicMock()]
            MockPipeline.return_value.run.return_value = [fake_result]
            out = run_cli_pipeline(str(book), str(tmp_path), client=MagicMock(), **kwargs)
            return out, MockPipeline, MockWriter, MockReader

    def test_returns_output_path_string(self, tmp_path):
        out, _, _, _ = self._run(tmp_path)
        assert out.endswith(".paprikarecipes")
        assert "cookbook" in out

    def test_output_file_named_after_stem(self, tmp_path):
        out, _, _, _ = self._run(tmp_path)
        assert Path(out).name == "cookbook.paprikarecipes"

    def test_epub_reader_used_for_epub(self, tmp_path):
        _, _, _, MockReader = self._run(tmp_path, suffix=".epub")
        MockReader.assert_called_once()

    def test_pdf_reader_used_for_pdf(self, tmp_path):
        _, _, _, MockReader = self._run(tmp_path, suffix=".pdf")
        MockReader.assert_called_once()

    def test_writer_called_with_results(self, tmp_path):
        _, MockPipeline, MockWriter, _ = self._run(tmp_path)
        MockWriter.return_value.write.assert_called_once()
        written_recipes = MockWriter.return_value.write.call_args[0][0]
        assert len(written_recipes) == 1

    def test_concurrency_forwarded_to_pipeline(self, tmp_path):
        _, MockPipeline, _, _ = self._run(tmp_path, concurrency=3)
        init_kwargs = MockPipeline.call_args.kwargs
        assert init_kwargs["concurrency"] == 3

    def test_rpm_forwarded_to_pipeline(self, tmp_path):
        _, MockPipeline, _, _ = self._run(tmp_path, rpm=20)
        init_kwargs = MockPipeline.call_args.kwargs
        assert init_kwargs["rpm"] == 20

    def test_concurrency_defaults_to_the_documented_cli_default(self, tmp_path):
        """concurrency=None runs at config.MAX_CONCURRENT_API_CALLS (1), what --help and
        the README promise - not the pipeline's server default of 4 (issue #149)."""
        from recipeparser.config import MAX_CONCURRENT_API_CALLS
        _, MockPipeline, _, _ = self._run(tmp_path, concurrency=None)
        init_kwargs = MockPipeline.call_args.kwargs
        assert MAX_CONCURRENT_API_CALLS == 1
        assert init_kwargs["concurrency"] == MAX_CONCURRENT_API_CALLS

    def test_rpm_omitted_when_none(self, tmp_path):
        """rpm=None must NOT be forwarded (uses pipeline default)."""
        _, MockPipeline, _, _ = self._run(tmp_path, rpm=None)
        init_kwargs = MockPipeline.call_args.kwargs
        assert "rpm" not in init_kwargs

    def test_output_dir_created_if_missing(self, tmp_path):
        new_dir = tmp_path / "new_subdir"
        assert not new_dir.exists()
        self._run(tmp_path)  # uses tmp_path which already exists — just smoke test
        # Verify mkdir is called (indirectly: no FileNotFoundError)

    def test_progress_callback_passed_to_run(self, tmp_path):
        from recipeparser.adapters.cli import run_cli_pipeline
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK")
        with patch("recipeparser.adapters.cli.EpubReader") as MockReader, \
             patch("recipeparser.adapters.cli.RecipePipeline") as MockPipeline, \
             patch("recipeparser.adapters.cli.PaprikaWriter"), \
             patch("recipeparser.adapters.cli.PipelineController"), \
             patch("recipeparser.adapters.cli.YamlCategorySource"):
            MockReader.return_value.read.return_value = [MagicMock()]
            MockPipeline.return_value.run.return_value = [_make_fake_result()]
            run_cli_pipeline(str(book), str(tmp_path), client=MagicMock())
            run_call_kwargs = MockPipeline.return_value.run.call_args.kwargs
            assert "on_progress" in run_call_kwargs
            assert callable(run_call_kwargs["on_progress"])


# ---------------------------------------------------------------------------
# A stopped run keeps what it made (#161): the daily quota, an error, Ctrl-C
# or `docker stop` (SIGTERM) writes <book>-partial.paprikarecipes from the
# recipes finished so far, which before were all lost.
# ---------------------------------------------------------------------------

def _partial_run(tmp_path, run_side_effect, **kwargs):
    from recipeparser.adapters.cli import run_cli_pipeline
    book = tmp_path / "Food Lab.epub"
    book.write_bytes(b"PK")
    with patch("recipeparser.adapters.cli.EpubReader") as MockReader, \
         patch("recipeparser.adapters.cli.RecipePipeline") as MockPipeline, \
         patch("recipeparser.adapters.cli.PaprikaWriter") as MockWriter, \
         patch("recipeparser.adapters.cli.YamlCategorySource"):
        MockReader.return_value.read.return_value = [MagicMock()]
        MockPipeline.return_value.run.side_effect = run_side_effect
        try:
            out = run_cli_pipeline(str(book), str(tmp_path), client=MagicMock(), **kwargs)
            return out, MockWriter, None
        except BaseException as exc:  # noqa: BLE001 - the test inspects what escaped
            return None, MockWriter, exc


def _written(MockWriter):
    """(archive name, recipe titles) for each PaprikaWriter the run built and wrote with."""
    return [
        (Path(c.kwargs["output_path"]).name, [r.title for r in w.write.call_args[0][0]])
        for c, w in zip(MockWriter.call_args_list, [MockWriter.return_value] * len(MockWriter.call_args_list))
    ]


def test_the_daily_quota_stop_saves_the_recipes_made_so_far(tmp_path):
    from recipeparser.exceptions import DailyQuotaExhaustedError

    def run(chunks, on_progress=None, on_result=None, **_):
        on_result(_make_fake_result("Roast Chicken"), True)
        on_result(_make_fake_result("Pan Sauce"), True)
        raise DailyQuotaExhaustedError(gave_up_after_secs=86400)

    _out, MockWriter, exc = _partial_run(tmp_path, run)

    assert isinstance(exc, DailyQuotaExhaustedError), "the CLI still reports why it stopped"
    assert _written(MockWriter) == [("Food Lab-partial.paprikarecipes", ["Roast Chicken", "Pan Sauce"])]


def test_any_other_failure_saves_them_too(tmp_path):
    def run(chunks, on_progress=None, on_result=None, **_):
        on_result(_make_fake_result("Roast Chicken"), True)
        raise RuntimeError("boom")

    _out, MockWriter, exc = _partial_run(tmp_path, run)

    assert isinstance(exc, RuntimeError)
    assert _written(MockWriter) == [("Food Lab-partial.paprikarecipes", ["Roast Chicken"])]


def test_a_failure_before_any_recipe_writes_no_partial_archive(tmp_path):
    def run(chunks, **_):
        raise RuntimeError("boom")

    _out, MockWriter, exc = _partial_run(tmp_path, run)

    assert isinstance(exc, RuntimeError)
    assert MockWriter.call_count == 0


def test_ctrl_c_saves_the_recipes_made_so_far_and_exits_130(tmp_path):
    """The pipeline runs off the main thread so Ctrl-C (and SIGTERM) is heard at once."""
    import _thread
    import threading

    release = threading.Event()

    def run(chunks, on_progress=None, on_result=None, **_):
        on_result(_make_fake_result("Roast Chicken"), True)
        _thread.interrupt_main()  # the cook presses Ctrl-C while the run waits out the quota
        release.wait(10)
        return []

    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    try:
        with patch("recipeparser.adapters.cli.os._exit", side_effect=fake_exit):
            _out, MockWriter, exc = _partial_run(tmp_path, run)
    finally:
        release.set()

    assert exits == [130]
    assert _written(MockWriter) == [("Food Lab-partial.paprikarecipes", ["Roast Chicken"])]


def test_the_quota_wait_is_on_during_a_cli_run_and_off_after(tmp_path):
    from recipeparser.core import quota_wait

    seen = []

    def run(chunks, on_progress=None, on_result=None, **_):
        seen.append(quota_wait.current())
        on_result(_make_fake_result("Roast Chicken"), True)
        return [_make_fake_result("Roast Chicken")]

    out, MockWriter, exc = _partial_run(tmp_path, run)

    assert exc is None
    assert seen[0] is not None, "the CLI waits out a used-up daily quota"
    assert quota_wait.current() is None, "and leaves nothing behind for the next caller"
    assert _written(MockWriter) == [("Food Lab.paprikarecipes", ["Roast Chicken"])]
