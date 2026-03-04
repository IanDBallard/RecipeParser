"""Tests for recipeparser.pipeline — deduplication, PipelineContext, rate limiter, and process_epub config."""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_recipe
from recipeparser.config import MAX_CONCURRENT_CAP
from recipeparser.pipeline import (
    _RPMRateLimiter,
    deduplicate_recipes,
    PipelineContext,
    process_epub,
)


# ---------------------------------------------------------------------------
# deduplicate_recipes
# ---------------------------------------------------------------------------

class TestDeduplicateRecipes:

    def test_no_duplicates_unchanged(self):
        recipes = [make_recipe("Pasta"), make_recipe("Salad"), make_recipe("Soup")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 3

    def test_exact_duplicate_removed(self):
        recipes = [make_recipe("Pasta"), make_recipe("Pasta")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_case_insensitive_dedup(self):
        recipes = [
            make_recipe("Chocolate Cake"),
            make_recipe("chocolate cake"),
            make_recipe("CHOCOLATE CAKE"),
        ]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_leading_trailing_whitespace_normalised(self):
        recipes = [make_recipe("  Banana Bread  "), make_recipe("Banana Bread")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_first_occurrence_kept(self):
        r1 = make_recipe("Omelette", photo="omelette1.jpg")
        r2 = make_recipe("Omelette", photo="omelette2.jpg")
        result = deduplicate_recipes([r1, r2])
        assert result[0].photo_filename == "omelette1.jpg"

    def test_empty_list(self):
        assert deduplicate_recipes([]) == []

    def test_distinct_recipes_all_kept(self):
        recipes = [make_recipe(f"Recipe {i}") for i in range(10)]
        result = deduplicate_recipes(recipes)
        assert len(result) == 10


# ---------------------------------------------------------------------------
# PipelineContext
# ---------------------------------------------------------------------------

class TestPipelineContext:

    def test_context_stores_all_fields(self):
        client = MagicMock()
        sem = threading.Semaphore(5)
        ctx = PipelineContext(
            client=client,
            semaphore=sem,
            units="metric",
            category_tree=[("Soup", None)],
            paprika_cats=["Soup"],
        )
        assert ctx.client is client
        assert ctx.semaphore is sem
        assert ctx.units == "metric"
        assert ctx.category_tree == [("Soup", None)]
        assert ctx.paprika_cats == ["Soup"]

    def test_context_units_default_accessible(self):
        ctx = PipelineContext(
            client=MagicMock(),
            semaphore=threading.Semaphore(1),
            units="book",
            category_tree=[],
            paprika_cats=[],
        )
        assert ctx.units == "book"


# ---------------------------------------------------------------------------
# _RPMRateLimiter
# ---------------------------------------------------------------------------

class TestRPMRateLimiter:

    def test_allows_up_to_rpm_calls_without_sleep(self):
        """First `rpm` calls return immediately; time.sleep is not called."""
        limiter = _RPMRateLimiter(rpm=3)
        times = [0.0, 0.01, 0.02]
        with patch("recipeparser.pipeline.time.monotonic", side_effect=times), \
             patch("recipeparser.pipeline.time.sleep") as mock_sleep:
            for _ in range(3):
                limiter.wait_then_record_start()
        mock_sleep.assert_not_called()

    def test_blocks_until_window_slides_then_allows_next(self):
        """When over the cap, wait_then_record_start sleeps once then succeeds after time advances."""
        limiter = _RPMRateLimiter(rpm=3)
        # First 3 calls use 0, 0.01, 0.02. 4th call: now=0.03 → 3 in window → sleep(60 - 0.03).
        # Next loop iteration: now=60.02 → oldest (0) pruned → 2 in window → record and return.
        monotonic_returns = [0.0, 0.01, 0.02, 0.03, 60.02]
        with patch("recipeparser.pipeline.time.monotonic", side_effect=monotonic_returns), \
             patch("recipeparser.pipeline.time.sleep") as mock_sleep:
            for _ in range(4):
                limiter.wait_then_record_start()
        mock_sleep.assert_called_once()
        call_arg = mock_sleep.call_args[0][0]
        assert 59.9 <= call_arg <= 60.0


# ---------------------------------------------------------------------------
# process_epub concurrency cap and rpm
# ---------------------------------------------------------------------------

class TestProcessEpubConfig:

    def test_concurrency_capped_at_max(self, tmp_path):
        """process_epub(concurrency=20) uses Semaphore(MAX_CONCURRENT_CAP) for the pipeline cap."""
        epub_path = tmp_path / "tiny.epub"
        epub_path.write_bytes(b"PK\x03\x04")  # minimal zip
        with patch("recipeparser.pipeline.load_epub") as mock_load_epub:
            mock_load_epub.return_value = ("Book", str(tmp_path), set(), [])
            with patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True), \
                 patch("recipeparser.pipeline.threading.Semaphore") as mock_sem:
                with patch("recipeparser.pipeline.create_paprika_export", return_value=str(tmp_path / "out.paprikarecipes")):
                    process_epub(
                        str(epub_path), str(tmp_path), MagicMock(),
                        concurrency=20, rpm=None,
                    )
            # Pipeline creates one Semaphore(cap) for the context; there may be other Semaphore(0) calls elsewhere.
            calls = mock_sem.call_args_list
            assert any(c[0][0] == MAX_CONCURRENT_CAP for c in calls), f"Expected a Semaphore({MAX_CONCURRENT_CAP}) call, got {calls}"

    def test_rpm_creates_rate_limiter(self, tmp_path):
        """When rpm is set, a rate limiter is created and passed in context (no segments, so no workers run)."""
        epub_path = tmp_path / "tiny.epub"
        epub_path.write_bytes(b"PK\x03\x04")
        with patch("recipeparser.pipeline.load_epub") as mock_load_epub:
            mock_load_epub.return_value = ("Book", str(tmp_path), set(), [])
            with patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True), \
                 patch("recipeparser.pipeline._RPMRateLimiter") as mock_rpm_class:
                with patch("recipeparser.pipeline.create_paprika_export", return_value=str(tmp_path / "out.paprikarecipes")):
                    process_epub(
                        str(epub_path), str(tmp_path), MagicMock(),
                        concurrency=2, rpm=10,
                    )
                mock_rpm_class.assert_called_once_with(10)

    def test_toc_extracted_for_recon_raw_chunking_always_used(self, tmp_path):
        """TOC is extracted when available; raw chunking is always used; recon runs when TOC exists."""
        epub_path = tmp_path / "tiny.epub"
        epub_path.write_bytes(b"PK\x03\x04")
        raw_chunks = [
            "Chicken Soup\nIngredients: 1 cup broth, 2 tbsp butter\nDirections: heat",
            "Beef Stew\nIngredients: 500g beef, 1 tsp salt\nDirections: simmer",
        ]
        toc_entries = [("Chicken Soup", 1), ("Beef Stew", 2)]

        with (
            patch("recipeparser.pipeline.load_epub") as mock_load,
            patch("recipeparser.pipeline.extract_toc_epub") as mock_extract,
            patch("recipeparser.pipeline.run_recon") as mock_recon,
            patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True),
            patch("recipeparser.pipeline.gem.extract_recipes") as mock_ext,
            patch("recipeparser.pipeline.create_paprika_export", return_value=True),
        ):
            mock_load.return_value = ("Book", str(tmp_path), set(), raw_chunks)
            mock_extract.return_value = toc_entries
            mock_ext.return_value = MagicMock(
                recipes=[make_recipe("Chicken Soup"), make_recipe("Beef Stew")]
            )
            mock_recon.return_value = (["Chicken Soup", "Beef Stew"], [], [])
            process_epub(str(epub_path), str(tmp_path), MagicMock(), concurrency=2)

        mock_extract.assert_called_once()
        mock_recon.assert_called_once()
        call_args = mock_recon.call_args[0]
        assert call_args[0] == toc_entries
        assert call_args[1] == ["Chicken Soup", "Beef Stew"]

    def test_empty_toc_skips_recon(self, tmp_path):
        """When TOC extraction returns fewer than MIN_TOC_ENTRIES, recon is skipped."""
        epub_path = tmp_path / "tiny.epub"
        epub_path.write_bytes(b"PK\x03\x04")
        raw_chunks = ["Intro\nChicken Soup\nIngredients..."]
        toc_entries = [("Chicken Soup", 1)]  # 1 entry < MIN_TOC_ENTRIES (2)

        with (
            patch("recipeparser.pipeline.load_epub") as mock_load,
            patch("recipeparser.pipeline.extract_toc_epub") as mock_extract,
            patch("recipeparser.pipeline.run_recon") as mock_recon,
            patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True),
            patch("recipeparser.pipeline.gem.extract_recipes") as mock_ext,
            patch("recipeparser.pipeline.create_paprika_export", return_value=True),
        ):
            mock_load.return_value = ("Book", str(tmp_path), set(), raw_chunks)
            mock_extract.return_value = toc_entries  # only 1 entry
            mock_ext.return_value = MagicMock(recipes=[make_recipe("Chicken Soup")])
            process_epub(str(epub_path), str(tmp_path), MagicMock(), concurrency=2)

        mock_recon.assert_not_called()

    def test_no_toc_skips_recon(self, tmp_path):
        """When TOC extraction returns empty list, recon is skipped."""
        epub_path = tmp_path / "tiny.epub"
        epub_path.write_bytes(b"PK\x03\x04")

        with (
            patch("recipeparser.pipeline.load_epub") as mock_load,
            patch("recipeparser.pipeline.extract_toc_epub") as mock_extract,
            patch("recipeparser.pipeline.run_recon") as mock_recon,
            patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True),
            patch("recipeparser.pipeline.gem.extract_recipes") as mock_ext,
            patch("recipeparser.pipeline.create_paprika_export", return_value=True),
        ):
            mock_load.return_value = ("Book", str(tmp_path), set(), ["chunk"])
            mock_extract.return_value = []
            mock_ext.return_value = MagicMock(recipes=[make_recipe("Recipe")])
            process_epub(str(epub_path), str(tmp_path), MagicMock(), concurrency=2)

        mock_recon.assert_not_called()
