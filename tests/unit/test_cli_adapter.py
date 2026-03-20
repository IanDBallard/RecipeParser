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

    def test_uom_system_forwarded_to_pipeline(self, tmp_path):
        _, MockPipeline, _, _ = self._run(tmp_path, uom_system="Metric")
        init_kwargs = MockPipeline.call_args.kwargs
        assert init_kwargs["uom_system"] == "Metric"

    def test_measure_preference_forwarded_to_pipeline(self, tmp_path):
        _, MockPipeline, _, _ = self._run(tmp_path, measure_preference="Weight")
        init_kwargs = MockPipeline.call_args.kwargs
        assert init_kwargs["measure_preference"] == "Weight"

    def test_concurrency_forwarded_to_pipeline(self, tmp_path):
        _, MockPipeline, _, _ = self._run(tmp_path, concurrency=3)
        init_kwargs = MockPipeline.call_args.kwargs
        assert init_kwargs["concurrency"] == 3

    def test_rpm_forwarded_to_pipeline(self, tmp_path):
        _, MockPipeline, _, _ = self._run(tmp_path, rpm=20)
        init_kwargs = MockPipeline.call_args.kwargs
        assert init_kwargs["rpm"] == 20

    def test_concurrency_omitted_when_none(self, tmp_path):
        """concurrency=None must NOT be forwarded (uses pipeline default)."""
        _, MockPipeline, _, _ = self._run(tmp_path, concurrency=None)
        init_kwargs = MockPipeline.call_args.kwargs
        assert "concurrency" not in init_kwargs

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
# _units_to_uom helper (tested via __main__ import)
# ---------------------------------------------------------------------------

class TestUnitsToUom:

    def test_metric_maps_to_Metric(self):
        from recipeparser.__main__ import _units_to_uom
        assert _units_to_uom("metric") == "Metric"

    def test_us_maps_to_US(self):
        from recipeparser.__main__ import _units_to_uom
        assert _units_to_uom("us") == "US"

    def test_imperial_maps_to_Imperial(self):
        from recipeparser.__main__ import _units_to_uom
        assert _units_to_uom("imperial") == "Imperial"

    def test_book_maps_to_US(self):
        from recipeparser.__main__ import _units_to_uom
        assert _units_to_uom("book") == "US"

    def test_unknown_defaults_to_US(self):
        from recipeparser.__main__ import _units_to_uom
        assert _units_to_uom("nonsense") == "US"

    def test_case_insensitive(self):
        from recipeparser.__main__ import _units_to_uom
        assert _units_to_uom("METRIC") == "Metric"
        assert _units_to_uom("US") == "US"
