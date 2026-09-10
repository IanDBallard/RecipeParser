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
