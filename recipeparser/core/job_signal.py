"""One running job's cancel flag and its last sign of life (Cayenne F-426).

An ingest job's work happens on worker threads several calls away from the
endpoint that owns it: the photo reader's vision OCR, the pipeline's chunk
workers, and the Gemini retry ladder under both. A ``JobSignal`` is how those
places learn that the job's owner pressed Cancel, and how the liveness loop
learns that the job is still doing something.

It travels in a ``ContextVar``, set once around the whole job. ``asyncio.to_thread``
and the pipeline's ``contextvars.copy_context().run`` both carry it to the worker
threads (the same route ``usage.attributed`` takes), so no reader or stage needs
a new parameter. Outside a job there is no signal, and every function here is a
no-op: the CLI, the GUI and the tests run exactly as before.

No I/O here (core/ layer rule): the row write that makes a cancel durable is the
API's; this is only the in-process half.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from typing import Callable, Iterator, Optional

from recipeparser.exceptions import JobCancelledError


class JobSignal:
    """The cancel flag and activity clock of one job. Safe to use from any thread."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._last_activity = clock()
        # Depth of shielded() blocks open on any thread: the wind-down after a
        # cancel (tagging what was already written) must not be cancelled itself.
        self._shield_depth = 0

    # ── cancel ────────────────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Ask the job to stop at its next checkpoint, and wake any wait()."""
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise ``JobCancelledError`` once cancelled, unless inside ``shielded()``."""
        with self._lock:
            shielded = self._shield_depth > 0
        if self._cancelled.is_set() and not shielded:
            raise JobCancelledError("The import was cancelled.")

    @contextlib.contextmanager
    def shielded(self) -> Iterator[None]:
        """Let the block run to its end even after a cancel."""
        with self._lock:
            self._shield_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._shield_depth -= 1

    # ── activity ──────────────────────────────────────────────────────────────

    def touch(self) -> None:
        """Record that the job is doing something (a Gemini attempt, a wait, a stage)."""
        now = self._clock()
        with self._lock:
            self._last_activity = now

    def idle_seconds(self) -> float:
        """How long since the last ``touch()``."""
        with self._lock:
            last = self._last_activity
        return max(0.0, self._clock() - last)

    # ── waiting ───────────────────────────────────────────────────────────────

    def wait(self, seconds: float) -> None:
        """Sleep ``seconds``, waking at once on a cancel, which then raises.

        Touches before and after, so a back-off wait is a sign of life, not a
        silence the liveness loop could mistake for a hung job.
        """
        self.touch()
        self.raise_if_cancelled()
        if seconds > 0:
            self._cancelled.wait(seconds)
        self.touch()
        self.raise_if_cancelled()


_current: contextvars.ContextVar[Optional[JobSignal]] = contextvars.ContextVar(
    "recipeparser_job_signal", default=None
)


def current() -> Optional[JobSignal]:
    """The signal of the job this code runs for, or None outside a job."""
    return _current.get()


@contextlib.contextmanager
def active(signal: JobSignal) -> Iterator[JobSignal]:
    """Make ``signal`` the current job's for the block (and the threads it starts)."""
    token = _current.set(signal)
    try:
        yield signal
    finally:
        _current.reset(token)


def checkpoint() -> None:
    """A place the job may stop: records activity, then raises if it was cancelled."""
    signal = _current.get()
    if signal is None:
        return
    signal.touch()
    signal.raise_if_cancelled()


def touch() -> None:
    """Record activity on the current job, if there is one."""
    signal = _current.get()
    if signal is not None:
        signal.touch()


def sleep(seconds: float) -> None:
    """``time.sleep`` outside a job; inside one, a wait a cancel cuts short."""
    signal = _current.get()
    if signal is None:
        time.sleep(seconds)
        return
    signal.wait(seconds)


@contextlib.contextmanager
def shielded() -> Iterator[None]:
    """``JobSignal.shielded`` on the current job; a no-op outside one."""
    signal = _current.get()
    if signal is None:
        yield
        return
    with signal.shielded():
        yield
