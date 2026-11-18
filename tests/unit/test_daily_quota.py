"""A used-up daily Gemini quota (RecipeParser #149's reporter, #161).

Gemini's free tier allows 500 requests a day per model. When they are spent it answers
429 RESOURCE_EXHAUSTED with a QuotaFailure naming a per-day quota and a RetryInfo delay
of hours. The back-off ladder retried after 2, 4, 8 and 16 seconds, and the run lost
its 81 finished chunks when it was stopped. Now:

- the CLI (a ``QuotaWait`` is set) waits for Gemini's delay, or 30 minutes when none is
  given, and tries again, giving up after 24 hours;
- with no ``QuotaWait`` (the server) the call raises ``DailyQuotaExhaustedError`` at once.
"""
from __future__ import annotations

import threading
from typing import List

import pytest
from google.genai import errors as genai_errors

from recipeparser import gemini
from recipeparser.core import quota_wait
from recipeparser.core.quota_wait import QuotaWait
from recipeparser.exceptions import DailyQuotaExhaustedError

PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"


def _quota_429(quota_id: str = PER_DAY, retry: str | None = "17040s", message: str | None = None):
    details: list = [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": quota_id,
            "quotaValue": "500",
        }],
    }]
    if retry is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return genai_errors.ClientError(429, {"error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": message or "You exceeded your current quota, please check your plan and billing details.",
        "details": details,
    }})


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)
        self.now += secs


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def waiting(clock):
    """The CLI's policy, on a fake clock, set for the test and removed after it."""
    policy = QuotaWait(clock=clock, sleep=clock.sleep)
    quota_wait.install(policy)
    yield policy
    quota_wait.install(None)


@pytest.fixture(autouse=True)
def _no_policy_leak():
    yield
    quota_wait.install(None)


# --- Recognising it --------------------------------------------------------------------

def test_a_per_day_quota_is_the_daily_quota():
    assert gemini.daily_quota_retry_after(_quota_429()) == 17040.0


def test_a_per_minute_quota_is_not():
    assert gemini.daily_quota_retry_after(_quota_429(PER_MINUTE, "31s")) is None


def test_a_long_retry_delay_alone_marks_it_daily():
    """A 429 whose QuotaFailure names no period but asks for hours is not a per-minute limit."""
    assert gemini.daily_quota_retry_after(_quota_429("SomeQuota", "7200s")) == 7200.0


def test_the_delay_is_read_from_the_message_when_retryinfo_is_missing():
    exc = _quota_429(retry=None, message="Quota exceeded for metric ... limit: 500\nPlease retry in 17040.5s.")
    assert gemini.daily_quota_retry_after(exc) == 17040.5


def _brians_429(with_retry_info: bool = True):
    """The 429 from the reporter's Food Lab run (#161's log, 19:15:18), as Gemini sent it."""
    details = [
        {"@type": "type.googleapis.com/google.rpc.Help", "links": [
            {"description": "Learn more about Gemini API quotas", "url": "https://aistudio.google.com/docs/rate-limits"}]},
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "description": "Generate content requests per day limit exceeded on Free Tier. "
                           "See https://aistudio.google.com/docs/rate-limits.",
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "quotaDimensions": {"location": "global", "model": "gemini-3.1-flash-lite"},
            "quotaValue": "500"}]},
    ]
    if with_retry_info:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17081s"})
    return genai_errors.ClientError(429, {"error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details. For more "
                   "information on this error, head to: https://aistudio.google.com/docs/rate-limits. To monitor "
                   "your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: "
                   "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 500, model: "
                   "gemini-3.1-flash-lite, description: Generate content requests per day limit exceeded on Free "
                   "Tier. See https://aistudio.google.com/docs/rate-limits.\nPlease retry in 4h44m41.229156774s.",
        "status": "RESOURCE_EXHAUSTED",
        "details": details,
    }})


def test_the_reporters_own_429_is_the_daily_quota():
    assert gemini.daily_quota_retry_after(_brians_429()) == 17081.0


def test_without_retryinfo_the_delay_is_read_from_geminis_hours_and_minutes():
    assert gemini.daily_quota_retry_after(_brians_429(with_retry_info=False)) == pytest.approx(17081.229, abs=0.01)


def test_a_daily_quota_with_no_delay_is_still_daily():
    """Recognised, with the delay unknown: the waiter falls back to its own interval."""
    assert gemini.daily_quota_retry_after(_quota_429(retry=None)) == 0.0


def test_other_errors_are_not_the_daily_quota():
    assert gemini.daily_quota_retry_after(ValueError("quota")) is None
    assert gemini.daily_quota_retry_after(
        genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}})
    ) is None


# --- The server: no policy, stop at once ---------------------------------------------------

def test_without_a_policy_the_daily_quota_raises_at_once():
    calls = []

    def call():
        calls.append(1)
        raise _quota_429()

    with pytest.raises(DailyQuotaExhaustedError) as caught:
        gemini._with_backoff(call)
    assert len(calls) == 1, "the 2-4-8-16 s ladder is not spent on a quota that resets in hours"
    assert caught.value.retry_after_secs == 17040.0
    assert "4 h 44 min" in str(caught.value)


def test_a_per_minute_429_still_climbs_the_ladder(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    errors = [_quota_429(PER_MINUTE, "31s"), _quota_429(PER_MINUTE, "31s")]

    def call():
        if errors:
            raise errors.pop()
        return "ok"

    assert gemini._with_backoff(call) == "ok"


# --- The CLI: wait and try again -----------------------------------------------------------

def test_with_a_policy_it_waits_geminis_delay_then_carries_on(waiting, clock):
    errors = [_quota_429()]

    def call():
        if errors:
            raise errors.pop()
        return "ok"

    assert gemini._with_backoff(call) == "ok"
    assert clock.slept == [17040.0 + 60.0], "Gemini's delay plus a minute"


def test_with_no_delay_given_it_tries_every_thirty_minutes(waiting, clock):
    errors = [_quota_429(retry=None)] * 3

    def call():
        if errors:
            raise errors.pop()
        return "ok"

    assert gemini._with_backoff(call, max_retries=1) == "ok", "waiting spends no retry attempts"
    assert clock.slept == [1800.0, 1800.0, 1800.0]


def test_it_gives_up_after_twenty_four_hours(waiting, clock):
    def call():
        raise _quota_429(retry=None)

    with pytest.raises(DailyQuotaExhaustedError, match="24 h"):
        gemini._with_backoff(call)
    assert sum(clock.slept) == pytest.approx(24 * 3600)


def test_a_success_starts_the_next_days_wait_afresh(waiting, clock):
    """The Food Lab needs more than one day's quota: each day gets its own 24 hours."""
    policy = waiting
    policy.wait(None)
    clock.now += 23 * 3600
    policy.succeeded()
    policy.wait(None)  # would exceed 24 h if the first episode still counted
    assert len(clock.slept) == 2


def test_threads_that_hit_the_quota_together_share_one_pause(clock):
    """Two workers at concurrency 2 must not each start (and log) their own wait."""
    starts: List[float] = []
    release = threading.Event()

    def sleep(secs: float) -> None:
        starts.append(secs)
        release.wait(5)

    policy = QuotaWait(clock=clock, sleep=sleep)
    threads = [threading.Thread(target=policy.wait, args=(600.0,)) for _ in range(2)]
    for t in threads:
        t.start()
    release.set()
    for t in threads:
        t.join(5)

    paused = policy.pauses
    assert paused == 1
