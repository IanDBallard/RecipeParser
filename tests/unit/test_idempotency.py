"""One answer per Idempotency-Key (Cayenne Fix Roadmap F-200)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from recipeparser.adapters.idempotency import BAD_KEY, IN_FLIGHT, IdempotencyCache

KEY = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _cache(now):
    return IdempotencyCache(ttl=100.0, pending_ttl=10.0, max_entries=3, clock=lambda: now[0])


def test_no_key_is_a_no_op_every_time():
    cache = IdempotencyCache()
    for _ in range(2):
        with cache.once("u1", None) as slot:
            assert slot.replay is None
            slot.done({"job_id": "j"})


def test_a_finished_request_is_replayed():
    now = [0.0]
    cache = _cache(now)
    with cache.once("u1", KEY) as slot:
        assert slot.replay is None
        slot.done({"job_id": "j1"})
    with cache.once("u1", KEY) as slot:
        assert slot.replay == {"job_id": "j1"}


def test_the_key_is_per_user():
    cache = IdempotencyCache()
    with cache.once("u1", KEY) as slot:
        slot.done({"job_id": "j1"})
    with cache.once("u2", KEY) as slot:
        assert slot.replay is None


def test_a_request_still_running_is_409():
    cache = IdempotencyCache()
    with cache.once("u1", KEY):
        with pytest.raises(HTTPException) as caught:
            with cache.once("u1", KEY):
                pass
    assert caught.value.status_code == 409
    assert caught.value.detail == IN_FLIGHT


def test_a_refused_request_frees_its_key():
    cache = IdempotencyCache()
    with pytest.raises(HTTPException):
        with cache.once("u1", KEY):
            raise HTTPException(status_code=422, detail="no")
    with cache.once("u1", KEY) as slot:
        assert slot.replay is None


def test_a_block_that_never_calls_done_frees_its_key():
    cache = IdempotencyCache()
    with cache.once("u1", KEY):
        pass
    with cache.once("u1", KEY) as slot:
        assert slot.replay is None


def test_a_stored_answer_expires():
    now = [0.0]
    cache = _cache(now)
    with cache.once("u1", KEY) as slot:
        slot.done({"job_id": "j1"})
    now[0] = 101.0
    with cache.once("u1", KEY) as slot:
        assert slot.replay is None


def test_an_abandoned_pending_claim_is_free_after_pending_ttl():
    now = [0.0]
    cache = _cache(now)
    cache._claim("u1", KEY)  # a claim whose handler never finished
    now[0] = 11.0
    with cache.once("u1", KEY) as slot:
        assert slot.replay is None


def test_the_oldest_entry_goes_past_max_entries():
    now = [0.0]
    cache = _cache(now)
    for i in range(4):
        now[0] = float(i)
        with cache.once("u1", f"key-{i:04d}") as slot:
            slot.done({"n": i})
    with cache.once("u1", "key-0000") as slot:
        assert slot.replay is None
    with cache.once("u1", "key-0003") as slot:
        assert slot.replay == {"n": 3}


@pytest.mark.parametrize("bad", ["short", "x" * 129, "has space-in-it", "émoji-key-1", "abcdefgh\n"])
def test_a_malformed_key_is_422(bad):
    with pytest.raises(HTTPException) as caught:
        with IdempotencyCache().once("u1", bad):
            pass
    assert caught.value.status_code == 422
    assert caught.value.detail == BAD_KEY


def test_a_raise_after_done_stores_nothing():
    cache = IdempotencyCache()
    with pytest.raises(RuntimeError):
        with cache.once("u1", KEY) as slot:
            slot.done({"job_id": "j1"})
            raise RuntimeError("failed after the answer was recorded")
    with cache.once("u1", KEY) as slot:
        assert slot.replay is None


def test_a_replay_is_a_copy():
    cache = IdempotencyCache()
    answer = {"job_id": "j1"}
    with cache.once("u1", KEY) as slot:
        slot.done(answer)
    answer["job_id"] = "changed by the caller"
    with cache.once("u1", KEY) as slot:
        assert slot.replay == {"job_id": "j1"}
        slot.replay["job_id"] = "changed by a replay caller"
    with cache.once("u1", KEY) as slot:
        assert slot.replay == {"job_id": "j1"}


@pytest.mark.parametrize("stale_raises", [False, True])
def test_a_stale_handler_cannot_disturb_a_newer_claim(stale_raises):
    now = [0.0]
    cache = _cache(now)
    stale = cache.once("u1", KEY)
    stale_slot = stale.__enter__()  # claim A
    now[0] = 11.0  # A's pending claim outlives pending_ttl (10)
    with cache.once("u1", KEY) as fresh:  # claim B re-claims the key
        assert fresh.replay is None
        if stale_raises:
            stale.__exit__(RuntimeError, RuntimeError("late failure"), None)
        else:
            stale_slot.done({"job_id": "A"})
            stale.__exit__(None, None, None)
        with pytest.raises(HTTPException) as caught:  # B still holds the key
            with cache.once("u1", KEY):
                pass
        assert caught.value.status_code == 409
        fresh.done({"job_id": "B"})
    with cache.once("u1", KEY) as slot:
        assert slot.replay == {"job_id": "B"}
