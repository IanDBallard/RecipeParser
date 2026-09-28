"""REGEN_WORKER_ENABLED gates the background workers (spec 5.1)."""
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISABLE_AUTH", "1")
os.environ.setdefault("TEST_USER_ID", "00000000-0000-4000-8000-000000000001")

from fastapi.testclient import TestClient  # noqa: E402

import recipeparser.adapters.api as api  # noqa: E402


def test_worker_enabled_parsing():
    assert api._worker_enabled({"REGEN_WORKER_ENABLED": "1"})
    assert api._worker_enabled({"REGEN_WORKER_ENABLED": "true"})
    assert not api._worker_enabled({"REGEN_WORKER_ENABLED": "0"})
    assert not api._worker_enabled({})


def test_lifespan_does_not_start_workers_by_default(monkeypatch):
    monkeypatch.delenv("REGEN_WORKER_ENABLED", raising=False)
    with patch("recipeparser.adapters.regen_worker.run_workers", new=AsyncMock()) as run:
        with TestClient(api.app):
            pass
    run.assert_not_called()


def test_lifespan_starts_and_stops_workers(monkeypatch):
    monkeypatch.setenv("REGEN_WORKER_ENABLED", "1")
    run = AsyncMock()
    with patch("recipeparser.adapters.regen_worker.run_workers", new=run), \
         patch.object(api, "_get_supabase_service_client", return_value=MagicMock()), \
         patch.object(api, "_get_client", return_value=MagicMock()):
        with TestClient(api.app):
            pass
    run.assert_awaited_once()
    workers, stop = run.await_args.args[0], run.await_args.args[1]
    assert [type(w).__name__ for w in workers] == ["RegenWorker", "RecatWorker"]
    assert stop.is_set()


def test_lifespan_shutdown_is_bounded_when_a_worker_ignores_stop(monkeypatch):
    """Fix Roadmap F-007. run_workers checks ``stop`` only between polls, and one poll can be
    a whole REFINE. The shutdown awaited the task bare, so a container stop waited it out -- past
    Docker's 10 s grace, where it is killed rather than stopped. The wait is now bounded, and a
    task still running at the bound is cancelled."""
    import asyncio
    import time

    state = {"cancelled": False, "finished": False}

    async def busy_poll(workers, stop):
        # Stands in for a poll in flight: it does not look at `stop`.
        try:
            await asyncio.sleep(3)
            state["finished"] = True
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setenv("REGEN_WORKER_ENABLED", "1")
    monkeypatch.setattr(api, "BACKGROUND_STOP_SECONDS", 0.2)
    with patch("recipeparser.adapters.regen_worker.run_workers", new=busy_poll), \
         patch.object(api, "_get_supabase_service_client", return_value=MagicMock()), \
         patch.object(api, "_get_client", return_value=MagicMock()):
        with TestClient(api.app):
            started = time.monotonic()
        elapsed = time.monotonic() - started
    assert state["cancelled"] and not state["finished"]
    assert elapsed < 2, f"shutdown took {elapsed:.1f} s"


def test_lifespan_refuses_to_start_when_flag_set_without_supabase(monkeypatch):
    """The flag on with no service-role client is the silent failure the roadmap names: a server
    that answers every request correctly and drains nothing. It must not start."""
    monkeypatch.setenv("REGEN_WORKER_ENABLED", "1")
    with patch("recipeparser.adapters.regen_worker.run_workers", new=AsyncMock()) as run, \
         patch.object(api, "_get_supabase_service_client", return_value=None):
        with pytest.raises(RuntimeError, match="REGEN_WORKER_ENABLED is set"):
            with TestClient(api.app):
                pass
    run.assert_not_called()


def test_lifespan_warns_when_flag_unset(monkeypatch, caplog):
    """Unset is legal — a dev server — but never quiet: an edited recipe stays stale until a worker runs."""
    monkeypatch.delenv("REGEN_WORKER_ENABLED", raising=False)
    with patch("recipeparser.adapters.regen_worker.run_workers", new=AsyncMock()):
        with caplog.at_level(logging.WARNING):
            with TestClient(api.app) as client:
                assert client.get("/health").json()["regen_workers"] == "disabled"
    assert "REGEN_WORKER_ENABLED is not set" in caplog.text


# ---------------------------------------------------------------------------
# /health publishes the worker state
#
# The gate "REGEN_WORKER_ENABLED=1 on the live API" was answerable only by
# reading the startup log of a process that may have rotated it away. The same
# argument health() already makes for auth_mode applies here: publish the state
# rather than infer it.
# ---------------------------------------------------------------------------

def test_health_reports_workers_disabled_when_flag_unset(monkeypatch):
    monkeypatch.delenv("REGEN_WORKER_ENABLED", raising=False)
    with patch("recipeparser.adapters.regen_worker.run_workers", new=AsyncMock()):
        with TestClient(api.app) as client:
            assert client.get("/health").json()["regen_workers"] == "disabled"


def test_health_reports_workers_started(monkeypatch):
    monkeypatch.setenv("REGEN_WORKER_ENABLED", "1")
    with patch("recipeparser.adapters.regen_worker.run_workers", new=AsyncMock()), \
         patch.object(api, "_get_supabase_service_client", return_value=MagicMock()), \
         patch.object(api, "_get_client", return_value=MagicMock()):
        with TestClient(api.app) as client:
            assert client.get("/health").json()["regen_workers"] == "started"


# ---------------------------------------------------------------------------
# The suite owns its environment
#
# A developer's .env carries REGEN_WORKER_ENABLED=1 so their container runs the
# workers. load_dotenv() puts it in os.environ for the test session too, where
# _get_supabase_service_client() always returns None (live_writes_blocked), so
# the lifespan hit its "flag set, no client" refusal and 7 tests/test_api.py
# tests failed on that machine while CI, which has no .env, stayed green.
# ---------------------------------------------------------------------------

def test_the_session_does_not_inherit_a_developer_worker_flag():
    assert not api._worker_enabled(os.environ)


def test_the_app_starts_when_no_test_touches_the_worker_flag():
    """Every test that builds a TestClient without monkeypatching the flag depends on this."""
    with patch("recipeparser.adapters.regen_worker.run_workers", new=AsyncMock()):
        with TestClient(api.app) as client:
            assert client.get("/health").json()["regen_workers"] == "disabled"
