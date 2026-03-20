"""Tests for PipelineController FSM (Phase 3b/3c + Phase 7A stage callbacks)."""
import json
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from recipeparser.core.fsm import PipelineController, PipelineStatus, _TRANSITIONS, StageChangeCallback
from recipeparser.exceptions import (
    CheckpointError,
    PipelineTransitionError,
    RateLimitPauseError,
)
from recipeparser.config import RATE_LIMIT_PAUSE_THRESHOLD, CHECKPOINT_SUBDIR


# ---------------------------------------------------------------------------
# FSM transition table
# ---------------------------------------------------------------------------

class TestFSMTransitions:

    def test_idle_start_transitions_to_running(self):
        ctrl = PipelineController()
        assert ctrl.status == PipelineStatus.IDLE
        result = ctrl.transition("start")
        assert result is True
        assert ctrl.status == PipelineStatus.RUNNING

    def test_running_pause_transitions_to_pausing(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        result = ctrl.transition("pause")
        assert result is True
        assert ctrl.status == PipelineStatus.PAUSING

    def test_pausing_paused_transitions_to_paused(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        result = ctrl.transition("paused")
        assert result is True
        assert ctrl.status == PipelineStatus.PAUSED

    def test_paused_resume_transitions_to_resuming(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        ctrl.transition("paused")
        result = ctrl.transition("resume")
        assert result is True
        assert ctrl.status == PipelineStatus.RESUMING

    def test_resuming_running_transitions_to_running(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        ctrl.transition("paused")
        ctrl.transition("resume")
        result = ctrl.transition("running")
        assert result is True
        assert ctrl.status == PipelineStatus.RUNNING

    def test_running_cancel_transitions_to_cancelling(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        result = ctrl.transition("cancel")
        assert result is True
        assert ctrl.status == PipelineStatus.CANCELLING

    def test_pausing_cancel_transitions_to_cancelling(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        result = ctrl.transition("cancel")
        assert result is True
        assert ctrl.status == PipelineStatus.CANCELLING

    def test_paused_cancel_transitions_to_cancelling(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        ctrl.transition("paused")
        result = ctrl.transition("cancel")
        assert result is True
        assert ctrl.status == PipelineStatus.CANCELLING

    def test_running_done_transitions_to_idle(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        result = ctrl.transition("done")
        assert result is True
        assert ctrl.status == PipelineStatus.IDLE

    def test_running_error_transitions_to_idle(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        result = ctrl.transition("error")
        assert result is True
        assert ctrl.status == PipelineStatus.IDLE

    def test_pausing_error_transitions_to_idle(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        result = ctrl.transition("error")
        assert result is True
        assert ctrl.status == PipelineStatus.IDLE

    def test_resuming_error_transitions_to_idle(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        ctrl.transition("paused")
        ctrl.transition("resume")
        result = ctrl.transition("error")
        assert result is True
        assert ctrl.status == PipelineStatus.IDLE

    def test_invalid_transition_returns_false(self):
        ctrl = PipelineController()
        # IDLE → pause is not defined
        result = ctrl.transition("pause")
        assert result is False
        assert ctrl.status == PipelineStatus.IDLE  # unchanged

    def test_invalid_transition_does_not_change_state(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("done")
        # IDLE → done is not defined
        result = ctrl.transition("done")
        assert result is False
        assert ctrl.status == PipelineStatus.IDLE

    def test_transition_or_raise_raises_on_invalid(self):
        ctrl = PipelineController()
        with pytest.raises(PipelineTransitionError):
            ctrl.transition_or_raise("pause")  # IDLE → pause invalid

    def test_transition_or_raise_succeeds_on_valid(self):
        ctrl = PipelineController()
        ctrl.transition_or_raise("start")  # should not raise
        assert ctrl.status == PipelineStatus.RUNNING

    def test_all_defined_transitions_are_reachable(self):
        """Sanity check: every key in _TRANSITIONS is a valid (state, event) pair."""
        for (state, event), next_state in _TRANSITIONS.items():
            assert isinstance(state, PipelineStatus)
            assert isinstance(event, str)
            assert isinstance(next_state, PipelineStatus)


# ---------------------------------------------------------------------------
# request_pause / request_resume / request_cancel
# ---------------------------------------------------------------------------

class TestPauseResumeCancel:

    def test_request_pause_from_running_returns_true(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        result = ctrl.request_pause()
        assert result is True
        assert ctrl.status == PipelineStatus.PAUSING

    def test_request_pause_clears_resume_event(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.request_pause()
        # _resume_event should be cleared (not set) so worker will block
        assert not ctrl._resume_event.is_set()

    def test_request_pause_from_idle_returns_false(self):
        ctrl = PipelineController()
        result = ctrl.request_pause()
        assert result is False
        assert ctrl.status == PipelineStatus.IDLE

    def test_request_resume_from_paused_returns_true(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        ctrl.transition("paused")
        result = ctrl.request_resume()
        assert result is True
        assert ctrl.status == PipelineStatus.RESUMING

    def test_request_resume_sets_resume_event(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.request_pause()
        ctrl.transition("paused")
        ctrl.request_resume()
        assert ctrl._resume_event.is_set()

    def test_request_resume_from_idle_returns_false(self):
        ctrl = PipelineController()
        result = ctrl.request_resume()
        assert result is False

    def test_request_cancel_from_running_returns_true(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        result = ctrl.request_cancel()
        assert result is True
        assert ctrl.status == PipelineStatus.CANCELLING

    def test_request_cancel_from_pausing_returns_true(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        result = ctrl.request_cancel()
        assert result is True
        assert ctrl.status == PipelineStatus.CANCELLING

    def test_request_cancel_from_paused_returns_true(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("pause")
        ctrl.transition("paused")
        result = ctrl.request_cancel()
        assert result is True
        assert ctrl.status == PipelineStatus.CANCELLING

    def test_request_cancel_sets_resume_event_to_unblock_worker(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.request_pause()
        ctrl.request_cancel()
        # resume_event must be set so a blocked worker can exit
        assert ctrl._resume_event.is_set()

    def test_request_cancel_from_idle_returns_false(self):
        ctrl = PipelineController()
        result = ctrl.request_cancel()
        assert result is False


# ---------------------------------------------------------------------------
# check_pause_point cooperative pause
# ---------------------------------------------------------------------------

class TestCheckPausePoint:

    def test_returns_true_when_running(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        assert ctrl.check_pause_point() is True

    def test_returns_true_when_idle(self):
        ctrl = PipelineController()
        # IDLE is not CANCELLING or PAUSING, so returns True
        assert ctrl.check_pause_point() is True

    def test_returns_false_when_cancelling(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.transition("cancel")
        assert ctrl.check_pause_point() is False

    def test_blocks_then_continues_on_resume(self):
        """Worker thread blocks in PAUSING state until resume is called."""
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.request_pause()  # → PAUSING, clears _resume_event

        results = []

        def worker():
            # This call should block until resumed
            result = ctrl.check_pause_point()
            results.append(result)

        t = threading.Thread(target=worker)
        t.start()

        # Give the worker time to enter check_pause_point and block
        time.sleep(0.05)
        assert t.is_alive(), "Worker should be blocked"

        # Resume from the main thread
        ctrl.request_resume()
        t.join(timeout=2.0)
        assert not t.is_alive(), "Worker should have unblocked"
        assert results == [True]
        assert ctrl.status == PipelineStatus.RUNNING

    def test_blocks_then_returns_false_on_cancel(self):
        """Worker thread blocks in PAUSING state and returns False when cancelled."""
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.request_pause()  # → PAUSING

        results = []

        def worker():
            result = ctrl.check_pause_point()
            results.append(result)

        t = threading.Thread(target=worker)
        t.start()

        time.sleep(0.05)
        assert t.is_alive(), "Worker should be blocked"

        ctrl.request_cancel()
        t.join(timeout=2.0)
        assert not t.is_alive(), "Worker should have unblocked"
        assert results == [False]


# ---------------------------------------------------------------------------
# Rate-limit 429 tracking (Phase 3c)
# ---------------------------------------------------------------------------

class TestRateLimitTracking:

    def test_record_429_below_threshold_does_not_raise(self):
        ctrl = PipelineController()
        # Should not raise for the first (threshold - 1) calls
        for _ in range(RATE_LIMIT_PAUSE_THRESHOLD - 1):
            ctrl.record_429()  # no exception

    def test_record_429_at_threshold_raises(self):
        ctrl = PipelineController()
        with pytest.raises(RateLimitPauseError):
            for _ in range(RATE_LIMIT_PAUSE_THRESHOLD):
                ctrl.record_429()

    def test_record_429_error_message_mentions_count(self):
        ctrl = PipelineController()
        with pytest.raises(RateLimitPauseError) as exc_info:
            for _ in range(RATE_LIMIT_PAUSE_THRESHOLD):
                ctrl.record_429()
        assert str(RATE_LIMIT_PAUSE_THRESHOLD) in str(exc_info.value)

    def test_reset_429_counter_allows_new_threshold(self):
        ctrl = PipelineController()
        # Hit threshold - 1 times
        for _ in range(RATE_LIMIT_PAUSE_THRESHOLD - 1):
            ctrl.record_429()
        # Reset
        ctrl.reset_429_counter()
        # Should be able to hit threshold - 1 again without raising
        for _ in range(RATE_LIMIT_PAUSE_THRESHOLD - 1):
            ctrl.record_429()  # no exception

    def test_reset_429_counter_resets_to_zero(self):
        ctrl = PipelineController()
        for _ in range(RATE_LIMIT_PAUSE_THRESHOLD - 1):
            ctrl.record_429()
        ctrl.reset_429_counter()
        assert ctrl._consecutive_429s == 0

    def test_trigger_rate_limit_pause_transitions_to_paused(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        # Use a very long resume_secs so the timer doesn't fire during the test
        ctrl.trigger_rate_limit_pause(resume_secs=86400)
        assert ctrl.status == PipelineStatus.PAUSED
        # Clean up the timer
        ctrl._cancel_auto_resume_timer()

    def test_trigger_rate_limit_pause_schedules_timer(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.trigger_rate_limit_pause(resume_secs=86400)
        assert ctrl._auto_resume_timer is not None
        ctrl._cancel_auto_resume_timer()

    def test_trigger_rate_limit_pause_auto_resumes(self):
        """With a very short delay, the auto-resume timer fires and resumes."""
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.trigger_rate_limit_pause(resume_secs=0)
        # Give the timer thread time to fire
        time.sleep(0.2)
        assert ctrl.status == PipelineStatus.RESUMING

    def test_request_resume_cancels_auto_resume_timer(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.trigger_rate_limit_pause(resume_secs=86400)
        assert ctrl._auto_resume_timer is not None
        ctrl.request_resume()
        # Timer should have been cancelled
        assert ctrl._auto_resume_timer is None

    def test_request_cancel_cancels_auto_resume_timer(self):
        ctrl = PipelineController()
        ctrl.transition("start")
        ctrl.trigger_rate_limit_pause(resume_secs=86400)
        assert ctrl._auto_resume_timer is not None
        ctrl.request_cancel()
        assert ctrl._auto_resume_timer is None


# ---------------------------------------------------------------------------
# Checkpoint persistence
# ---------------------------------------------------------------------------

class TestCheckpointPersistence:

    def test_save_and_load_checkpoint_round_trip(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)

        ctrl.save_checkpoint(
            str(book),
            stage="EXTRACT",
            completed_segments=[0, 1, 2],
            extracted_recipes=[{"name": "Pasta"}],
            toc_entries=[["Pasta", 10]],
        )

        data = ctrl.load_checkpoint(str(book))
        assert data is not None
        assert data["stage"] == "EXTRACT"
        assert data["completed_segments"] == [0, 1, 2]
        assert data["extracted_recipes"] == [{"name": "Pasta"}]
        assert data["toc_entries"] == [["Pasta", 10]]

    def test_checkpoint_file_created_in_subdir(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)

        ctrl.save_checkpoint(str(book), "LOAD", [], [], [])

        cp_dir = tmp_path / CHECKPOINT_SUBDIR
        assert cp_dir.is_dir()
        files = list(cp_dir.glob("*.json"))
        assert len(files) == 1

    def test_checkpoint_contains_version_and_hash(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)

        ctrl.save_checkpoint(str(book), "EXTRACT", [], [], [])

        cp_dir = tmp_path / CHECKPOINT_SUBDIR
        cp_file = next(cp_dir.glob("*.json"))
        data = json.loads(cp_file.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["book_hash"].startswith("sha256:")
        assert "timestamp" in data

    def test_load_checkpoint_returns_none_when_no_file(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "nonexistent.epub"
        result = ctrl.load_checkpoint(str(book))
        assert result is None

    def test_load_checkpoint_returns_none_without_output_dir(self, tmp_path):
        ctrl = PipelineController()  # no output_dir
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)
        result = ctrl.load_checkpoint(str(book))
        assert result is None

    def test_save_checkpoint_silently_skips_without_output_dir(self, tmp_path):
        ctrl = PipelineController()  # no output_dir
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)
        # Should not raise
        ctrl.save_checkpoint(str(book), "EXTRACT", [], [], [])

    def test_load_checkpoint_returns_none_on_version_mismatch(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)

        ctrl.save_checkpoint(str(book), "EXTRACT", [], [], [])

        # Corrupt the version in the checkpoint file
        cp_dir = tmp_path / CHECKPOINT_SUBDIR
        cp_file = next(cp_dir.glob("*.json"))
        data = json.loads(cp_file.read_text(encoding="utf-8"))
        data["version"] = 999
        cp_file.write_text(json.dumps(data), encoding="utf-8")

        result = ctrl.load_checkpoint(str(book))
        assert result is None

    def test_load_checkpoint_returns_none_on_hash_mismatch(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)

        ctrl.save_checkpoint(str(book), "EXTRACT", [], [], [])

        # Corrupt the hash in the checkpoint file
        cp_dir = tmp_path / CHECKPOINT_SUBDIR
        cp_file = next(cp_dir.glob("*.json"))
        data = json.loads(cp_file.read_text(encoding="utf-8"))
        data["book_hash"] = "sha256:deadbeef"
        cp_file.write_text(json.dumps(data), encoding="utf-8")

        result = ctrl.load_checkpoint(str(book))
        assert result is None

    def test_load_checkpoint_raises_on_malformed_json(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)

        ctrl.save_checkpoint(str(book), "EXTRACT", [], [], [])

        cp_dir = tmp_path / CHECKPOINT_SUBDIR
        cp_file = next(cp_dir.glob("*.json"))
        cp_file.write_text("NOT VALID JSON", encoding="utf-8")

        with pytest.raises(CheckpointError):
            ctrl.load_checkpoint(str(book))

    def test_delete_checkpoint_removes_file(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)

        ctrl.save_checkpoint(str(book), "EXTRACT", [], [], [])
        cp_dir = tmp_path / CHECKPOINT_SUBDIR
        assert len(list(cp_dir.glob("*.json"))) == 1

        ctrl.delete_checkpoint(str(book))
        assert len(list(cp_dir.glob("*.json"))) == 0

    def test_delete_checkpoint_is_idempotent(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)
        # Deleting when no checkpoint exists should not raise
        ctrl.delete_checkpoint(str(book))

    def test_book_hash_is_stable(self, tmp_path):
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)
        h1 = PipelineController._book_hash(str(book))
        h2 = PipelineController._book_hash(str(book))
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_book_hash_differs_for_different_content(self, tmp_path):
        book_a = tmp_path / "a.epub"
        book_b = tmp_path / "b.epub"
        book_a.write_bytes(b"AAAA" * 100)
        book_b.write_bytes(b"BBBB" * 100)
        assert PipelineController._book_hash(str(book_a)) != PipelineController._book_hash(str(book_b))

    def test_save_checkpoint_raises_on_write_error(self, tmp_path):
        ctrl = PipelineController(output_dir=str(tmp_path))
        book = tmp_path / "cookbook.epub"
        book.write_bytes(b"PK" * 100)

        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            with pytest.raises(CheckpointError):
                ctrl.save_checkpoint(str(book), "EXTRACT", [], [], [])


# ---------------------------------------------------------------------------
# Stage-change callback (Phase 7A — §11.4)
# ---------------------------------------------------------------------------

class TestStageChangeCallback:
    """Tests for PipelineController.notify_stage_change() and on_stage_change wiring."""

    def test_notify_stage_change_calls_callback(self):
        """notify_stage_change fires the registered callback with the stage name."""
        received: list[str] = []
        ctrl = PipelineController(on_stage_change=lambda s: received.append(s))
        ctrl.notify_stage_change("EXTRACTING")
        assert received == ["EXTRACTING"]

    def test_notify_stage_change_fires_multiple_times(self):
        """Each call to notify_stage_change fires the callback once."""
        received: list[str] = []
        ctrl = PipelineController(on_stage_change=lambda s: received.append(s))
        for stage in ["EXTRACTING", "REFINING", "CATEGORIZING", "EMBEDDING"]:
            ctrl.notify_stage_change(stage)
        assert received == ["EXTRACTING", "REFINING", "CATEGORIZING", "EMBEDDING"]

    def test_notify_stage_change_no_callback_is_noop(self):
        """notify_stage_change with no callback registered does not raise."""
        ctrl = PipelineController()  # no on_stage_change
        ctrl.notify_stage_change("EXTRACTING")  # must not raise

    def test_notify_stage_change_reraises_callback_exception(self):
        """Callback failures re-raise (§11.4 — FAIL LOUDLY)."""
        def _bad_callback(stage: str) -> None:
            raise RuntimeError("Supabase write failed")

        ctrl = PipelineController(on_stage_change=_bad_callback)
        with pytest.raises(RuntimeError, match="Supabase write failed"):
            ctrl.notify_stage_change("EXTRACTING")

    def test_on_stage_change_accepts_callable_matching_protocol(self):
        """StageChangeCallback Protocol is satisfied by a plain callable."""
        mock = MagicMock()
        ctrl = PipelineController(on_stage_change=mock)
        ctrl.notify_stage_change("EMBEDDING")
        mock.assert_called_once_with("EMBEDDING")

    def test_stage_change_callback_is_independent_of_progress_callback(self):
        """on_stage_change and on_progress are independent — both fire correctly."""
        stage_calls: list[str] = []
        progress_calls: list[tuple] = []

        ctrl = PipelineController(
            on_stage_change=lambda s: stage_calls.append(s),
            on_progress=lambda s, c, t: progress_calls.append((s, c, t)),
        )
        ctrl.notify_stage_change("REFINING")
        ctrl.notify_progress("PROCESSING", 1, 5)

        assert stage_calls == ["REFINING"]
        assert progress_calls == [("PROCESSING", 1, 5)]

    def test_stage_change_callback_is_thread_safe(self):
        """notify_stage_change can be called from multiple threads without data loss."""
        received: list[str] = []
        lock = threading.Lock()

        def _thread_safe_cb(stage: str) -> None:
            with lock:
                received.append(stage)

        ctrl = PipelineController(on_stage_change=_thread_safe_cb)
        stages = ["EXTRACTING", "REFINING", "CATEGORIZING", "EMBEDDING"] * 10

        threads = [
            threading.Thread(target=ctrl.notify_stage_change, args=(s,))
            for s in stages
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert sorted(received) == sorted(stages)

    def test_on_stage_change_stored_on_controller(self):
        """The callback is stored as _on_stage_change on the controller instance."""
        cb = MagicMock()
        ctrl = PipelineController(on_stage_change=cb)
        assert ctrl._on_stage_change is cb

    def test_on_stage_change_none_by_default(self):
        """Without on_stage_change kwarg, _on_stage_change is None."""
        ctrl = PipelineController()
        assert ctrl._on_stage_change is None

    def test_stage_change_callback_satisfies_protocol(self):
        """A lambda satisfies the StageChangeCallback Protocol (runtime_checkable)."""
        cb = lambda s: None  # noqa: E731
        assert isinstance(cb, StageChangeCallback)
