"""
tests/unit/test_rate_limiter.py
────────────────────────────────
Gate tests for GlobalRateLimiter (Phase 2 deliverable).

Three mandatory gate tests:
  1. Singleton identity — two calls to GlobalRateLimiter() return the same object.
  2. Sliding-window enforcement — when the window is full, wait_then_record_start()
     blocks until a slot opens (verified with a patched time.sleep).
  3. reset() clears the window — after reset(), current_window_count == 0.

Each test calls reset() in setUp/tearDown to guarantee isolation regardless of
execution order.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from recipeparser.core.rate_limiter import GlobalRateLimiter


class TestGlobalRateLimiterSingleton(unittest.TestCase):
    """Gate test 1: singleton identity."""

    def setUp(self) -> None:
        GlobalRateLimiter().reset()

    def tearDown(self) -> None:
        GlobalRateLimiter().reset()

    def test_two_instantiations_return_same_object(self) -> None:
        """GlobalRateLimiter() must return the identical instance every time."""
        a = GlobalRateLimiter(rpm=10)
        b = GlobalRateLimiter(rpm=99)  # rpm arg ignored on second call
        self.assertIs(a, b)

    def test_singleton_survives_across_threads(self) -> None:
        """The singleton must be the same object when retrieved from a different thread."""
        instances: list[GlobalRateLimiter] = []

        def grab() -> None:
            instances.append(GlobalRateLimiter())

        t = threading.Thread(target=grab)
        t.start()
        t.join()

        self.assertIs(GlobalRateLimiter(), instances[0])


class TestGlobalRateLimiterWindow(unittest.TestCase):
    """Gate test 2: sliding-window enforcement."""

    def setUp(self) -> None:
        GlobalRateLimiter().reset()

    def tearDown(self) -> None:
        GlobalRateLimiter().reset()

    def test_slots_below_cap_are_granted_immediately(self) -> None:
        """Calls below the RPM cap must return without sleeping."""
        limiter = GlobalRateLimiter()
        limiter.reset()
        limiter._rpm = 5  # set directly — constructor arg ignored after first instantiation

        sleep_calls: list[float] = []
        with patch("recipeparser.core.rate_limiter.time.sleep", side_effect=sleep_calls.append):
            for _ in range(5):
                limiter.wait_then_record_start()

        self.assertEqual(sleep_calls, [], "No sleep should occur when under the cap")
        self.assertEqual(limiter.current_window_count, 5)

    def test_window_full_causes_sleep_then_retry(self) -> None:
        """When the window is full, wait_then_record_start() must sleep and retry.

        Strategy: force rpm=1 directly on the singleton, pre-fill the window
        with a timestamp 59 s ago (so it expires in ~1 s), then patch
        time.sleep to record the call and time.monotonic to advance by 2 s
        after the first sleep so the retry succeeds immediately.

        Note: the constructor rpm arg is ignored after first instantiation
        (singleton), so we set _rpm directly for test isolation.
        """
        limiter = GlobalRateLimiter()
        limiter.reset()
        limiter._rpm = 1  # force cap to 1 regardless of singleton init order

        # Pre-fill: one call 59 seconds ago (expires in ~1 second).
        fake_past = time.monotonic() - 59.0
        with limiter._lock:
            limiter._starts = [fake_past]

        sleep_calls: list[float] = []

        original_monotonic = time.monotonic

        def advancing_monotonic() -> float:
            """After the first sleep, pretend 2 extra seconds have passed."""
            base = original_monotonic()
            return base + (2.0 if sleep_calls else 0.0)

        def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        with (
            patch("recipeparser.core.rate_limiter.time.sleep", side_effect=fake_sleep),
            patch("recipeparser.core.rate_limiter.time.monotonic", side_effect=advancing_monotonic),
        ):
            limiter.wait_then_record_start()

        self.assertTrue(len(sleep_calls) >= 1, "Should have slept at least once")
        self.assertTrue(all(d >= 0 for d in sleep_calls), "Sleep duration must be non-negative")

    def test_concurrent_calls_never_exceed_cap(self) -> None:
        """Multiple threads calling wait_then_record_start() must not exceed rpm cap.

        Uses rpm=3 and 3 threads; all should succeed without exceeding the window.
        """
        limiter = GlobalRateLimiter()
        limiter.reset()
        limiter._rpm = 3  # set directly — constructor arg ignored after first instantiation

        errors: list[str] = []

        def call() -> None:
            try:
                limiter.wait_then_record_start()
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertLessEqual(
            limiter.current_window_count,
            3,
            "Window count must not exceed rpm cap",
        )


class TestGlobalRateLimiterReset(unittest.TestCase):
    """Gate test 3: reset() clears the window."""

    def setUp(self) -> None:
        GlobalRateLimiter().reset()

    def tearDown(self) -> None:
        GlobalRateLimiter().reset()

    def test_reset_clears_window(self) -> None:
        """After reset(), current_window_count must be 0."""
        limiter = GlobalRateLimiter()
        limiter.reset()
        limiter._rpm = 10  # set directly — constructor arg ignored after first instantiation

        # Fill the window partially.
        for _ in range(5):
            limiter.wait_then_record_start()

        self.assertEqual(limiter.current_window_count, 5)

        limiter.reset()

        self.assertEqual(limiter.current_window_count, 0)

    def test_reset_allows_immediate_calls_after_full_window(self) -> None:
        """After filling the window and calling reset(), new calls succeed without sleeping."""
        limiter = GlobalRateLimiter()
        limiter.reset()
        limiter._rpm = 3  # set directly — constructor arg ignored after first instantiation

        for _ in range(3):
            limiter.wait_then_record_start()

        limiter.reset()

        sleep_calls: list[float] = []
        with patch("recipeparser.core.rate_limiter.time.sleep", side_effect=sleep_calls.append):
            limiter.wait_then_record_start()

        self.assertEqual(sleep_calls, [], "No sleep after reset even when window was previously full")


if __name__ == "__main__":
    unittest.main()
