"""
recipeparser/core/fsm.py — PipelineController FSM (canonical location).

Moved here from recipeparser/pipeline.py as part of the Clean Architecture
migration (Phase 3).  pipeline.py retains a backward-compat shim that will
be deleted in Phase 7.

No I/O imports allowed in this module (core/ layer rule).
"""
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from recipeparser.config import (
    CHECKPOINT_SUBDIR,
    RATE_LIMIT_AUTO_RESUME_SECS,
    RATE_LIMIT_PAUSE_THRESHOLD,
)
from recipeparser.exceptions import (
    CheckpointError,
    PipelineTransitionError,
    RateLimitPauseError,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 3b/3c — PipelineController FSM with checkpoint and auto-pause
# ---------------------------------------------------------------------------

class PipelineStatus(Enum):
    """FSM states for the pipeline controller."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    CANCELLING = "cancelling"


# Valid (current_state, event) → next_state transitions
_TRANSITIONS: Dict[Tuple[PipelineStatus, str], PipelineStatus] = {
    (PipelineStatus.IDLE,       "start"):   PipelineStatus.RUNNING,
    (PipelineStatus.RUNNING,    "pause"):   PipelineStatus.PAUSING,
    (PipelineStatus.PAUSING,    "paused"):  PipelineStatus.PAUSED,
    (PipelineStatus.PAUSED,     "resume"):  PipelineStatus.RESUMING,
    (PipelineStatus.RESUMING,   "running"): PipelineStatus.RUNNING,
    (PipelineStatus.RUNNING,    "cancel"):  PipelineStatus.CANCELLING,
    (PipelineStatus.PAUSING,    "cancel"):  PipelineStatus.CANCELLING,
    (PipelineStatus.PAUSED,     "cancel"):  PipelineStatus.CANCELLING,
    (PipelineStatus.RUNNING,    "done"):    PipelineStatus.IDLE,
    (PipelineStatus.RUNNING,    "error"):   PipelineStatus.IDLE,
    (PipelineStatus.PAUSING,    "error"):   PipelineStatus.IDLE,
    (PipelineStatus.RESUMING,   "error"):   PipelineStatus.IDLE,
}

_CHECKPOINT_VERSION = 1


class PipelineController:
    """
    Finite-state machine that wraps a pipeline run with pause/resume/cancel
    support, checkpoint persistence, and auto-pause on repeated 429 errors.

    Thread safety
    -------------
    ``transition()`` and ``request_pause()`` / ``request_cancel()`` may be
    called from any thread (e.g. the GUI thread).  Internal state is protected
    by ``_lock``.  The pipeline worker thread calls ``check_pause_point()``
    cooperatively between segments.

    Checkpoint format (version 1)
    ------------------------------
    A JSON file at ``<output_dir>/<CHECKPOINT_SUBDIR>/<book_hash>.json``::

        {
          "version": 1,
          "book_path": "/path/to/book.epub",
          "book_hash": "sha256:...",
          "stage": "EXTRACT",
          "completed_segments": [0, 1, 2],
          "extracted_recipes": [],
          "toc_entries": [],
          "timestamp": "2026-03-08T14:00:00Z"
        }
    """

    def __init__(self, output_dir: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self.status: PipelineStatus = PipelineStatus.IDLE
        self._consecutive_429s: int = 0
        self._output_dir: Optional[Path] = Path(output_dir) if output_dir else None
        # Event used to block the worker thread while paused
        self._resume_event = threading.Event()
        self._resume_event.set()  # not paused initially
        # Timer handle for auto-resume after rate-limit pause
        self._auto_resume_timer: Optional[threading.Timer] = None

    # ── FSM ───────────────────────────────────────────────────────────────────

    def transition(self, event: str) -> bool:
        """
        Attempt a state transition triggered by ``event``.

        Returns True if the transition was valid and applied; False if the
        (current_state, event) pair has no defined transition (logs a warning).
        """
        with self._lock:
            key = (self.status, event)
            if key in _TRANSITIONS:
                old = self.status
                self.status = _TRANSITIONS[key]
                log.debug(
                    "PipelineController: %s --%s--> %s", old.value, event, self.status.value
                )
                return True
            log.warning(
                "PipelineController: invalid transition (%s, '%s') — ignored.",
                self.status.value, event,
            )
            return False

    def transition_or_raise(self, event: str) -> None:
        """Like ``transition()`` but raises ``PipelineTransitionError`` on failure."""
        if not self.transition(event):
            with self._lock:
                current = self.status.value
            raise PipelineTransitionError(
                f"No transition defined for state='{current}' event='{event}'."
            )

    # ── Pause / resume / cancel (called from GUI thread) ─────────────────────

    def request_pause(self) -> bool:
        """Signal the worker to pause at the next cooperative check point."""
        ok = self.transition("pause")
        if ok:
            self._resume_event.clear()
        return ok

    def request_resume(self) -> bool:
        """Resume a paused pipeline."""
        ok = self.transition("resume")
        if ok:
            self._cancel_auto_resume_timer()
            self._resume_event.set()
            # Worker will call transition("running") once it unblocks
        return ok

    def request_cancel(self) -> bool:
        """Cancel the pipeline (from any cancellable state)."""
        ok = self.transition("cancel")
        if ok:
            self._cancel_auto_resume_timer()
            self._resume_event.set()  # unblock worker so it can exit
        return ok

    # ── Cooperative pause point (called from worker thread) ───────────────────

    def check_pause_point(self) -> bool:
        """
        Called by the worker thread between segments.

        Blocks if the controller is in PAUSING state (waits for resume or
        cancel).  Returns True if the pipeline should continue, False if it
        has been cancelled and the worker should abort.
        """
        with self._lock:
            status = self.status

        if status == PipelineStatus.CANCELLING:
            return False

        if status == PipelineStatus.PAUSING:
            # Confirm we are now fully paused
            self.transition("paused")
            log.info("PipelineController: pipeline paused — waiting for resume or cancel.")
            # Block until resume_event is set
            self._resume_event.wait()
            # After unblocking, check whether we were cancelled or resumed
            with self._lock:
                post_status = self.status
            if post_status == PipelineStatus.CANCELLING:
                return False
            # Transition RESUMING → RUNNING (must be called outside _lock)
            self.transition("running")

        return True

    # ── Rate-limit tracking (Phase 3c) ────────────────────────────────────────

    def record_429(self) -> None:
        """
        Record a consecutive 429 response.  When the count reaches
        ``RATE_LIMIT_PAUSE_THRESHOLD``, raises ``RateLimitPauseError`` so the
        caller can trigger an auto-pause.
        """
        with self._lock:
            self._consecutive_429s += 1
            count = self._consecutive_429s

        log.warning(
            "PipelineController: 429 received (%d consecutive).", count
        )
        if count >= RATE_LIMIT_PAUSE_THRESHOLD:
            raise RateLimitPauseError(
                f"Received {count} consecutive 429 responses — auto-pausing for "
                f"{RATE_LIMIT_AUTO_RESUME_SECS // 3600}h."
            )

    def reset_429_counter(self) -> None:
        """Reset the consecutive-429 counter after a successful API call."""
        with self._lock:
            self._consecutive_429s = 0

    def trigger_rate_limit_pause(self, resume_secs: int = RATE_LIMIT_AUTO_RESUME_SECS) -> None:
        """
        Transition to PAUSED and schedule an auto-resume after ``resume_secs``.
        Called by the worker when it catches ``RateLimitPauseError``.
        """
        self.request_pause()
        # Immediately confirm paused (no cooperative check needed here)
        self.transition("paused")
        log.info(
            "PipelineController: rate-limit auto-pause — will auto-resume in %ds (%dh).",
            resume_secs, resume_secs // 3600,
        )
        self._cancel_auto_resume_timer()
        timer = threading.Timer(resume_secs, self._auto_resume)
        timer.daemon = True
        timer.start()
        self._auto_resume_timer = timer

    def _auto_resume(self) -> None:
        log.info("PipelineController: auto-resume timer fired.")
        self.request_resume()

    def _cancel_auto_resume_timer(self) -> None:
        if self._auto_resume_timer is not None:
            self._auto_resume_timer.cancel()
            self._auto_resume_timer = None

    # ── Checkpoint persistence ────────────────────────────────────────────────

    @staticmethod
    def _book_hash(book_path: str) -> str:
        """SHA-256 of the first 64 KB of the book file (fast, stable identifier)."""
        h = hashlib.sha256()
        try:
            with open(book_path, "rb") as f:
                h.update(f.read(65536))
        except OSError:
            h.update(book_path.encode())
        return f"sha256:{h.hexdigest()}"

    def _checkpoint_path(self, book_path: str) -> Optional[Path]:
        if self._output_dir is None:
            return None
        book_hash = self._book_hash(book_path)
        # Use last 16 hex chars of hash as filename to keep it short
        short = book_hash.split(":")[-1][:16]
        cp_dir = self._output_dir / CHECKPOINT_SUBDIR
        cp_dir.mkdir(parents=True, exist_ok=True)
        return cp_dir / f"{short}.json"

    def save_checkpoint(
        self,
        book_path: str,
        stage: str,
        completed_segments: List[int],
        extracted_recipes: List[Any],
        toc_entries: List[Any],
    ) -> None:
        """
        Persist current progress to a JSON checkpoint file.

        Raises ``CheckpointError`` if the file cannot be written.
        """
        cp_path = self._checkpoint_path(book_path)
        if cp_path is None:
            return  # no output_dir configured — skip silently

        data: Dict[str, Any] = {
            "version": _CHECKPOINT_VERSION,
            "book_path": str(book_path),
            "book_hash": self._book_hash(book_path),
            "stage": stage,
            "completed_segments": completed_segments,
            "extracted_recipes": extracted_recipes,
            "toc_entries": toc_entries,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            cp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log.debug("Checkpoint saved: %s", cp_path)
        except OSError as exc:
            raise CheckpointError(f"Could not write checkpoint to '{cp_path}': {exc}") from exc

    def load_checkpoint(self, book_path: str) -> Optional[Dict[str, Any]]:
        """
        Load a checkpoint for ``book_path`` if one exists and is valid.

        Returns the checkpoint dict, or None if no checkpoint is found.
        Raises ``CheckpointError`` if the file exists but is malformed.
        """
        cp_path = self._checkpoint_path(book_path)
        if cp_path is None or not cp_path.exists():
            return None

        try:
            data = json.loads(cp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"Could not read checkpoint '{cp_path}': {exc}"
            ) from exc

        if data.get("version") != _CHECKPOINT_VERSION:
            log.warning(
                "Checkpoint version mismatch (got %s, expected %d) — ignoring.",
                data.get("version"), _CHECKPOINT_VERSION,
            )
            return None

        # Verify the checkpoint belongs to the same file
        stored_hash = data.get("book_hash", "")
        current_hash = self._book_hash(book_path)
        if stored_hash != current_hash:
            log.warning(
                "Checkpoint hash mismatch — file may have changed. Ignoring checkpoint."
            )
            return None

        log.info(
            "Checkpoint loaded: stage=%s, %d completed segment(s).",
            data.get("stage"), len(data.get("completed_segments", [])),
        )
        return data

    def delete_checkpoint(self, book_path: str) -> None:
        """Remove the checkpoint file for ``book_path`` (called on successful completion)."""
        cp_path = self._checkpoint_path(book_path)
        if cp_path and cp_path.exists():
            try:
                cp_path.unlink()
                log.debug("Checkpoint deleted: %s", cp_path)
            except OSError as exc:
                log.warning("Could not delete checkpoint '%s': %s", cp_path, exc)
