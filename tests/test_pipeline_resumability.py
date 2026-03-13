"""Tests for PipelineController integration in process_epub — resumability, pause, and cancel."""
import os
import json
import threading
import time
from unittest.mock import MagicMock, patch
import pytest
from recipeparser.pipeline import process_epub, PipelineController, PipelineStatus, Stage
from recipeparser.models import RecipeExtraction, RecipeList

def _make_recipe(name):
    return RecipeExtraction(
        name=name,
        ingredients=["1 cup water", "2 tbsp oil"],
        directions=["Boil it.", "Stir well."],
        servings="1",
    )

# Strings that will pass is_recipe_candidate
def get_chunk(i):
    return f"Recipe {i}\nIngredients: 1 cup water, 2 tbsp oil\nDirections: Boil it. Stir well."

class TestPipelineResumability:

    def test_checkpoint_saving_and_loading(self, tmp_path):
        """Pipeline saves progress to checkpoint and can resume from it."""
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(b"PK\x03\x04")
        
        raw_chunks = [get_chunk(i) for i in range(5)]
        
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        
        controller = PipelineController(output_dir=str(output_dir))
        
        # 1. First run: process only some segments then "crash"
        with (
            patch("recipeparser.pipeline.load_epub") as mock_load,
            patch("recipeparser.pipeline.load_pdf") as mock_load_pdf,
            patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True),
            patch("recipeparser.pipeline.gem.extract_recipes") as mock_ext,
            # Return False so ExportError is raised and delete_checkpoint() is NOT called,
            # leaving the checkpoint file on disk for the assertion below.
            patch("recipeparser.pipeline.create_paprika_export", return_value=False),
            patch("recipeparser.pipeline.extract_toc_epub", return_value=[]),
            # Disable the 12s free-tier delay so the test runs in milliseconds.
            patch("recipeparser.pipeline.FREE_TIER_DELAY_SECS", 0),
        ):
            mock_load.return_value = ("Book", str(tmp_path / "images"), set(), raw_chunks)
            
            # Mock extractor to return recipe for CHUNK 0 then fail for others
            def side_effect(chunk, client, units="book"):
                if "Recipe 0" in chunk:
                    return RecipeList(recipes=[_make_recipe("Recipe 0")])
                raise RuntimeError("Simulated crash")
            
            mock_ext.side_effect = side_effect
            
            try:
                process_epub(str(epub_path), str(output_dir), MagicMock(), controller=controller, concurrency=1)
            except Exception:
                pass  # ExportError (or RuntimeError) expected — checkpoint must survive
        
        # Verify checkpoint exists (actual subdir is ".recipeparser_checkpoints")
        checkpoint_dir = output_dir / ".recipeparser_checkpoints"
        assert checkpoint_dir.exists()
        checkpoints = list(checkpoint_dir.glob("*.json"))
        assert len(checkpoints) == 1
        
        with open(checkpoints[0], "r") as f:
            cp_data = json.load(f)
            assert len(cp_data["completed_segments"]) >= 1
            
        # 2. Second run: resume from checkpoint
        controller = PipelineController(output_dir=str(output_dir))
        with (
            patch("recipeparser.pipeline.load_epub") as mock_load,
            patch("recipeparser.pipeline.load_pdf") as mock_load_pdf,
            patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True),
            patch("recipeparser.pipeline.gem.extract_recipes") as mock_ext,
            patch("recipeparser.pipeline.create_paprika_export", return_value=True),
            patch("recipeparser.pipeline.extract_toc_epub", return_value=[]),
        ):
            mock_load.return_value = ("Book", str(tmp_path / "images"), set(), raw_chunks)
            mock_ext.return_value = RecipeList(recipes=[_make_recipe("Success")])
            
            process_epub(str(epub_path), str(output_dir), MagicMock(), controller=controller, concurrency=1)
            
            # All segments were completed in run 1 (either success or fail)
            assert mock_ext.call_count == 0

    def test_pipeline_cancel(self, tmp_path):
        """Pipeline respects cancel signal from controller."""
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(b"PK\x03\x04")
        raw_chunks = [get_chunk(i) for i in range(3)]
        
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        
        controller = PipelineController(output_dir=str(output_dir))
        
        with (
            patch("recipeparser.pipeline.load_epub") as mock_load,
            patch("recipeparser.pipeline.load_pdf") as mock_load_pdf,
            patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True),
            patch("recipeparser.pipeline.gem.extract_recipes") as mock_ext,
            patch("recipeparser.pipeline.extract_toc_epub", return_value=[]),
            patch("recipeparser.pipeline.create_paprika_export", return_value=True),
            patch("recipeparser.pipeline.FREE_TIER_DELAY_SECS", 0),
        ):
            mock_load.return_value = ("Book", str(tmp_path / "images"), set(), raw_chunks)
            
            def cancelling_extractor(chunk, client, units):
                controller.request_cancel()
                return RecipeList(recipes=[_make_recipe("Recipe")])
            
            mock_ext.side_effect = cancelling_extractor
            
            result = process_epub(str(epub_path), str(output_dir), MagicMock(), controller=controller, concurrency=1)
            
            assert result == ""
            assert controller.status == PipelineStatus.CANCELLING

    def test_pipeline_pause_resume(self, tmp_path):
        """Pipeline can be paused and then resumed."""
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(b"PK\x03\x04")
        raw_chunks = [get_chunk(i) for i in range(10)]
        
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        
        controller = PipelineController(output_dir=str(output_dir))
        
        with (
            patch("recipeparser.pipeline.load_epub") as mock_load,
            patch("recipeparser.pipeline.load_pdf") as mock_load_pdf,
            patch("recipeparser.pipeline.gem.verify_connectivity", return_value=True),
            patch("recipeparser.pipeline.gem.extract_recipes") as mock_ext,
            patch("recipeparser.pipeline.extract_toc_epub", return_value=[]),
            patch("recipeparser.pipeline.create_paprika_export", return_value=True),
            # Disable the 12s free-tier delay — the slow_extractor's 0.5s sleep
            # is enough to give the test thread time to call request_pause().
            patch("recipeparser.pipeline.FREE_TIER_DELAY_SECS", 0),
        ):
            mock_load.return_value = ("Book", str(tmp_path / "images"), set(), raw_chunks)
            
            def slow_extractor(chunk, client, units):
                time.sleep(0.5)
                return RecipeList(recipes=[_make_recipe("R")])
            mock_ext.side_effect = slow_extractor
            
            def run():
                process_epub(str(epub_path), str(output_dir), MagicMock(), controller=controller, concurrency=1)
            
            t = threading.Thread(target=run, daemon=True)
            t.start()
            
            try:
                # Wait for it to start
                start_time = time.time()
                while controller.status == PipelineStatus.IDLE and time.time() - start_time < 5:
                    time.sleep(0.1)
                
                assert controller.status == PipelineStatus.RUNNING
                
                # Request pause
                controller.request_pause()
                
                # Wait for transition to PAUSED
                start_time = time.time()
                while controller.status != PipelineStatus.PAUSED and time.time() - start_time < 10:
                    time.sleep(0.1)
                    
                assert controller.status == PipelineStatus.PAUSED
                
                # Now resume
                controller.request_resume()
                t.join(timeout=15)
                assert not t.is_alive()
                assert controller.status == PipelineStatus.IDLE
            finally:
                # Ensure the pipeline thread is always unblocked on test failure
                # so pytest can exit cleanly (thread is daemon so it won't block exit).
                if controller.status in (PipelineStatus.PAUSING, PipelineStatus.PAUSED):
                    controller.request_cancel()
