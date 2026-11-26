"""
recipeparser/core/fsm.py — PipelineController FSM (canonical location).

Moved here from recipeparser/pipeline.py as part of the Clean Architecture
migration (Phase 3).  pipeline.py retains a backward-compat shim that will
be deleted in Phase 7.

No I/O imports allowed in this module (core/ layer rule).
"""
import logging
import threading
from enum import Enum
from typing import Dict, Optional, Protocol, Tuple, runtime_checkable

from recipeparser.exceptions import PipelineTransitionError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1 — ProgressCallback Protocol (§11.4)
# ---------------------------------------------------------------------------

@runtime_checkable
class ProgressCallback(Protocol):
    """
    Typed callback for pipeline progress notifications.

    Implementors receive a call on every meaningful stage transition so that
    adapters (GUI, API, CLI) can update their progress indicators without
    coupling to the FSM internals.

    Args:
        stage:     The current pipeline stage name (e.g. "EXTRACTING").
        completed: Number of items completed so far in this stage.
        total:     Total number of items expected in this stage.
                   May be 0 when the total is not yet known.
    """

    def __call__(self, stage: str, completed: int, total: int) -> None: ...


@runtime_checkable
class StageChangeCallback(Protocol):
    """
    Typed callback fired when the pipeline enters a new processing stage.

    This is distinct from ProgressCallback (which tracks chunk-level progress)
    — StageChangeCallback tracks which stage is currently executing within a
    chunk (EXTRACTING, REFINING, CATEGORIZING, EMBEDDING, ASSEMBLING).

    Adapters use this to update the ``ingestion_jobs.stage`` column in
    Supabase so the Cayenne app can show granular progress.

    Args:
        stage: The stage name matching the ``ingestion_jobs.stage`` CHECK
               constraint (e.g. "EXTRACTING", "REFINING", "EMBEDDING").
    """

    def __call__(self, stage: str) -> None: ...


# ---------------------------------------------------------------------------
# PipelineController FSM
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
    # After a cancelled run completes its wind-down, return to IDLE so the
    # controller can be reused for a subsequent run.
    (PipelineStatus.CANCELLING, "done"):    PipelineStatus.IDLE,
    (PipelineStatus.CANCELLING, "error"):   PipelineStatus.IDLE,
}

class PipelineController:
    """
    Finite-state machine that wraps a pipeline run with pause/resume/cancel
    support. It holds the transition table and the pause and cancel signalling,
    nothing else.

    Thread safety
    -------------
    ``transition()`` and ``request_pause()`` / ``request_cancel()`` may be
    called from any thread (e.g. the GUI thread).  Internal state is protected
    by ``_lock``.  The pipeline worker thread calls ``check_pause_point()``
    cooperatively between segments.
    """

    def __init__(
        self,
        on_progress: Optional[ProgressCallback] = None,
        on_stage_change: Optional[StageChangeCallback] = None,
    ) -> None:
        self._lock = threading.Lock()
        self.status: PipelineStatus = PipelineStatus.IDLE
        # Set by request_cancel() and never cleared — including by the
        # CANCELLING -> done -> IDLE wind-down RecipePipeline.run performs after
        # it breaks out of its loop. By the time the API builds the terminal
        # payload the status is IDLE and indistinguishable from a clean finish,
        # so the status cannot carry this and a flag has to. One controller is
        # constructed per job, so its lifetime is the job's; there is no reset.
        self.cancel_requested: bool = False
        # Typed progress callback — fired by notify_progress()
        self._on_progress: Optional[ProgressCallback] = on_progress
        # Stage-change callback — fired by notify_stage_change() before each stage
        self._on_stage_change: Optional[StageChangeCallback] = on_stage_change
        # Event used to block the worker thread while paused
        self._resume_event = threading.Event()
        self._resume_event.set()  # not paused initially

    # ── Progress / stage-change notifications ────────────────────────────────

    def notify_progress(self, stage: str, completed: int, total: int) -> None:
        """
        Fire the registered ``on_progress`` callback (if any).

        Safe to call from any thread. Callback failures **re-raise** after logging
        (PIPELINE_REFACTOR §11.4 — FAIL LOUDLY): swallowing adapter errors can
        leave zombie ingestion jobs that never reach ERROR state.
        """
        if self._on_progress is not None:
            try:
                self._on_progress(stage, completed, total)
            except Exception:  # noqa: BLE001
                log.exception(
                    "PipelineController: on_progress callback failed — re-raising (§11.4).",
                )
                raise

    def notify_stage_change(self, stage: str) -> None:
        """
        Fire the registered ``on_stage_change`` callback (if any).

        Called by ``RecipePipeline._process_chunk()`` immediately before
        entering each stage (EXTRACTING, REFINING, CATEGORIZING, EMBEDDING,
        ASSEMBLING).  Adapters use this to update ``ingestion_jobs.stage``
        in Supabase so the Cayenne app shows granular per-stage progress.

        Safe to call from any thread.  Callback failures **re-raise** after
        logging (§11.4 — FAIL LOUDLY): a silent failure here means the
        Cayenne app sees a stale stage label, which is a zombie-job symptom.

        Args:
            stage: Stage name matching the ``ingestion_jobs.stage`` CHECK
                   constraint (e.g. "EXTRACTING", "REFINING", "EMBEDDING").
        """
        if self._on_stage_change is not None:
            try:
                self._on_stage_change(stage)
            except Exception:  # noqa: BLE001
                log.exception(
                    "PipelineController: on_stage_change callback failed — re-raising (§11.4).",
                )
                raise

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
            self._resume_event.set()
            # Worker will call transition("running") once it unblocks
        return ok

    def request_cancel(self) -> bool:
        """Cancel the pipeline (from any cancellable state)."""
        ok = self.transition("cancel")
        if ok:
            # Only on a transition that actually happened: request_cancel from
            # IDLE returns False and must leave a never-cancellable job clean.
            # The write takes _lock in its own block rather than wrapping the
            # transition above — threading.Lock is not reentrant, and
            # transition() acquires it itself.
            with self._lock:
                self.cancel_requested = True
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
