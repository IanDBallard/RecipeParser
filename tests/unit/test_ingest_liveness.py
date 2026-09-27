"""Ingest liveness: a heartbeat for live imports, and a reaper for dead ones (Cayenne F-001).

An ingest job lives only in the process that runs it (_active_jobs). When that process dies the
row stayed `running` for ever: every device adopted it and could not start another import, and
deploy.sh refused every later api deploy. The heartbeat keeps a live job's row fresh; the reaper
ends any ingest row that has gone stale past the lease.
"""
import asyncio
import datetime
import os
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISABLE_AUTH", "1")
os.environ.setdefault("TEST_USER_ID", "00000000-0000-4000-8000-000000000001")


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, fake, table):
        self.fake, self.table, self.ops = fake, table, []

    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return _op

    def execute(self):
        self.fake.queries.append(self)
        if self.fake.raises is not None:
            raise self.fake.raises
        return _Result(self.fake.returns)


class FakeSupabase:
    def __init__(self, returns: List[Dict[str, Any]] | None = None):
        self.returns = returns or []
        self.raises: Exception | None = None
        self.queries: List[_Query] = []

    def table(self, name):
        return _Query(self, name)


NOW = datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _op(query, name):
    return [o for o in query.ops if o[0] == name]


def test_the_reaper_ends_only_stale_unfinished_ingest_rows():
    from recipeparser.adapters.ingest_liveness import INTERRUPTED_MESSAGE, LEASE_MINUTES, reap_stale_ingests

    fake = FakeSupabase(returns=[{"id": "j1"}, {"id": "j2"}])
    reaped = reap_stale_ingests(fake, NOW)

    assert reaped == ["j1", "j2"]
    (q,) = fake.queries
    assert q.table == "ingestion_jobs"
    (update,) = _op(q, "update")
    payload = update[1][0]
    assert payload["status"] == "error"
    assert payload["stage"] == "ERROR"
    assert payload["error_message"] == INTERRUPTED_MESSAGE
    # Recategorise resumes after a restart (RecatWorker reclaims it), so only ingest is ended.
    assert ("eq", ("kind", "ingest"), {}) in q.ops
    # pending too: a process can die between the row's insert and the job's first write.
    assert ("in_", ("status", ["pending", "running"]), {}) in q.ops
    cutoff = (NOW - datetime.timedelta(minutes=LEASE_MINUTES)).isoformat()
    assert ("lt", ("updated_at", cutoff), {}) in q.ops, \
        "unbounded by updated_at, the reaper would end a live job on another instance"


def test_the_heartbeat_touches_exactly_the_live_jobs():
    from recipeparser.adapters.ingest_liveness import touch_live_jobs

    fake = FakeSupabase()
    touch_live_jobs(fake, ["a", "b"], NOW)

    (q,) = fake.queries
    (update,) = _op(q, "update")
    assert set(update[1][0]) == {"updated_at"}, "a heartbeat must not change what the job reports"
    assert ("in_", ("id", ["a", "b"]), {}) in q.ops


def test_no_live_jobs_means_no_heartbeat_query():
    from recipeparser.adapters.ingest_liveness import touch_live_jobs

    fake = FakeSupabase()
    touch_live_jobs(fake, [], NOW)
    assert fake.queries == []


def test_a_tick_touches_its_own_jobs_before_it_reaps():
    """Reaping first could end this instance's own job, if its last write is older than the lease."""
    from recipeparser.adapters.ingest_liveness import tick

    fake = FakeSupabase()
    tick(fake, ["live"], NOW)

    kinds = [_op(q, "update")[0][1][0].get("status") for q in fake.queries]
    assert kinds == [None, "error"]


def test_a_failed_query_does_not_kill_the_loop(caplog):
    from recipeparser.adapters.ingest_liveness import tick

    fake = FakeSupabase()
    fake.raises = RuntimeError("network down")
    tick(fake, ["live"], NOW)  # must not raise
    assert "network down" in caplog.text


def test_the_loop_ticks_at_once_and_stops_when_told():
    from recipeparser.adapters import ingest_liveness

    fake = FakeSupabase()
    stop = asyncio.Event()
    seen: List[List[str]] = []

    def fake_tick(sb, live_ids, now):
        seen.append(list(live_ids))
        stop.set()

    async def main():
        with patch.object(ingest_liveness, "tick", side_effect=fake_tick):
            await asyncio.wait_for(
                ingest_liveness.run_liveness(fake, lambda: ["j1"], stop, tick_seconds=30), timeout=2
            )

    asyncio.run(main())
    assert seen == [["j1"]]


# ── lifespan wiring ───────────────────────────────────────────────────────────

import recipeparser.adapters.api as api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def test_the_lifespan_runs_liveness_whenever_it_can_write_even_with_workers_off(monkeypatch):
    """The regen flag gates regeneration, not this: a dead import blocks every device either way."""
    monkeypatch.delenv("REGEN_WORKER_ENABLED", raising=False)
    run = AsyncMock()
    with patch("recipeparser.adapters.ingest_liveness.run_liveness", new=run), \
         patch.object(api, "_get_supabase_service_client", return_value=MagicMock()):
        with TestClient(api.app):
            pass
    run.assert_awaited_once()
    live_ids, stop = run.await_args.args[1], run.await_args.args[2]
    api._active_jobs["probe"] = ("u", MagicMock())
    try:
        assert list(live_ids()) == ["probe"], "liveness must read the registry at each tick"
    finally:
        api._active_jobs.pop("probe", None)
    assert stop.is_set()


def test_the_lifespan_skips_liveness_without_a_service_client(monkeypatch):
    monkeypatch.delenv("REGEN_WORKER_ENABLED", raising=False)
    run = AsyncMock()
    with patch("recipeparser.adapters.ingest_liveness.run_liveness", new=run), \
         patch.object(api, "_get_supabase_service_client", return_value=None):
        with TestClient(api.app):
            pass
    run.assert_not_called()
