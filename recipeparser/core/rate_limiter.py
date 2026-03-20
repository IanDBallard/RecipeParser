"""
recipeparser/core/rate_limiter.py
──────────────────────────────────
Process-level singleton rate limiter for Gemini API calls.

Design
------
- True singleton: the same instance is returned by every ``GlobalRateLimiter()``
  call within a process, regardless of thread.
- Thread-safe: ``wait_then_record_start()`` uses a per-instance ``threading.Lock``
  to serialise access to the sliding-window timestamp list.
- Sliding window: only calls made within the last 60 seconds count toward the
  RPM cap.  When the window is full the caller sleeps until the oldest call
  ages out, then retries — no busy-loop.
- ``reset()`` is provided exclusively for test isolation; production code must
  never call it.

TID rule: this module lives in ``core/`` and therefore MUST NOT import from
``io/`` or ``adapters/``.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional


class GlobalRateLimiter:
    """Process-level singleton that enforces a requests-per-minute cap.

    Usage
    -----
    Call ``wait_then_record_start()`` immediately before every Gemini API
    request.  The call blocks until a slot is available, then records the
    start time and returns.

    Parameters
    ----------
    rpm:
        Maximum requests per minute.  Only honoured on the *first*
        instantiation; subsequent calls return the existing singleton
        unchanged.
    """

    _instance: Optional["GlobalRateLimiter"] = None
    _class_lock: threading.Lock = threading.Lock()

    # Instance attributes declared at class level so mypy strict can track them
    # across the __new__-based singleton pattern (set on `inst`, not `self`).
    _rpm: int
    _lock: threading.Lock
    _starts: List[float]

    def __new__(cls, rpm: int = 60) -> "GlobalRateLimiter":
        with cls._class_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._rpm = rpm
                inst._lock = threading.Lock()
                inst._starts = []
                cls._instance = inst
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def wait_then_record_start(self) -> None:
        """Block until a rate-limit slot is available, then claim it.

        Thread-safe.  Uses a sliding 60-second window.  When the window is
        full the method sleeps until the oldest recorded start ages out of
        the window, then retries.  This avoids busy-looping.
        """
        while True:
            now = time.monotonic()
            with self._lock:
                cutoff = now - 60.0
                self._starts = [t for t in self._starts if t > cutoff]
                if len(self._starts) < self._rpm:
                    self._starts.append(now)
                    return
                # Calculate how long to sleep before the oldest slot expires.
                sleep_until = min(self._starts) + 60.0 - now

            delay = max(0.0, sleep_until)
            if delay > 0:
                time.sleep(delay)

    def reset(self) -> None:
        """Clear the sliding window.  **For test isolation only.**

        Production code must never call this method.
        """
        with self._lock:
            self._starts = []

    # ------------------------------------------------------------------
    # Introspection helpers (read-only, for tests / observability)
    # ------------------------------------------------------------------

    @property
    def rpm(self) -> int:
        """The configured requests-per-minute cap."""
        return self._rpm

    @property
    def current_window_count(self) -> int:
        """Number of calls recorded in the current 60-second window."""
        now = time.monotonic()
        with self._lock:
            cutoff = now - 60.0
            return sum(1 for t in self._starts if t > cutoff)
