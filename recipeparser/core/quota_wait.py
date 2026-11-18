"""Waiting out a used-up daily Gemini quota, for the CLI only (#161).

Gemini's free tier allows 500 requests a day per model; a cookbook of a few hundred
chunks needs more than one day's. When the quota is spent, ``gemini._with_backoff``
hands the call to the installed ``QuotaWait``, which sleeps until Gemini says the
quota resets (plus a minute), or 30 minutes when it gives no time, and lets the call
try again. One pause is shared by every thread that meets the quota while it lasts.
After 24 hours of waiting without a success it gives up with
``DailyQuotaExhaustedError``; a success starts the next day's 24 hours afresh.

Nothing installs one on the server: a job that sleeps for hours looks hung to the
liveness check (Cayenne F-426), so there the quota stops the job at once.
"""
from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Callable, Optional

from recipeparser.exceptions import DailyQuotaExhaustedError, _duration

log = logging.getLogger(__name__)

#: Added to Gemini's own delay, so the retry lands after the reset rather than on it.
MARGIN_SECS = 60.0
#: How often to try when Gemini gives no delay, or the quota is still spent after one.
FALLBACK_SECS = 30 * 60.0
#: How long one stretch of waiting may last before the run gives up.
GIVE_UP_SECS = 24 * 3600.0


class QuotaWait:
    """The CLI's answer to a used-up daily quota: wait, and try again."""

    def __init__(
        self,
        *,
        give_up_after: float = GIVE_UP_SECS,
        fallback: float = FALLBACK_SECS,
        margin: float = MARGIN_SECS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_pause: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._give_up_after = give_up_after
        self._fallback = fallback
        self._margin = margin
        self._clock = clock
        self._sleep = sleep
        self._on_pause = on_pause
        self._lock = threading.Lock()
        self._resume_at: Optional[float] = None
        self._episode_started: Optional[float] = None
        self.pauses = 0  # times a new pause began, for the log and the tests

    def wait(self, retry_after: Optional[float]) -> None:
        """Sleep until the quota should have reset; raise once 24 hours have gone by.

        ``retry_after`` is Gemini's delay in seconds; 0 or None means it gave none.
        A thread that arrives while a pause is under way joins it.
        """
        with self._lock:
            now = self._clock()
            if self._resume_at is not None and now < self._resume_at:
                until = self._resume_at  # someone else's pause: join it
            else:
                if self._episode_started is None:
                    self._episode_started = now
                spent = now - self._episode_started
                if spent >= self._give_up_after:
                    raise DailyQuotaExhaustedError(retry_after or None, gave_up_after_secs=spent)
                delay = (retry_after + self._margin) if retry_after else self._fallback
                delay = min(delay, self._give_up_after - spent)
                until = self._resume_at = now + delay
                self.pauses += 1
                wall = datetime.datetime.now() + datetime.timedelta(seconds=delay)
                log.warning(
                    "Gemini's daily request quota is used up. Waiting until %s (%s), then trying again. "
                    "Press Ctrl-C to stop and save the recipes made so far.",
                    wall.strftime("%H:%M"), _duration(delay),
                )
                if self._on_pause is not None:
                    try:
                        self._on_pause(delay)
                    except Exception:  # a progress message must never stop the wait
                        log.exception("QuotaWait: on_pause callback failed.")
        remaining = until - self._clock()
        if remaining > 0:
            self._sleep(remaining)

    def succeeded(self) -> None:
        """A call went through: the quota is back, and the next wait starts its own 24 hours."""
        if self._episode_started is None:
            return
        with self._lock:
            self._episode_started = None
            self._resume_at = None


_installed: Optional[QuotaWait] = None


def install(policy: Optional[QuotaWait]) -> None:
    """Set (or, with None, remove) the process's policy. The CLI sets one; the server never does."""
    global _installed
    _installed = policy


def current() -> Optional[QuotaWait]:
    """The installed policy, or None: then a used-up daily quota raises at once."""
    return _installed
