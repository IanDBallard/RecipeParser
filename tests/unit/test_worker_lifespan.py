"""REGEN_WORKER_ENABLED gates the background workers (spec 5.1)."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

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
    assert [type(w).__name__ for w in workers] == ["RegenWorker", "RecatWorker"] or \
           [type(w).__name__ for w in workers] == ["RegenWorker"]
    assert stop.is_set()


def test_lifespan_skips_when_supabase_unavailable(monkeypatch):
    monkeypatch.setenv("REGEN_WORKER_ENABLED", "1")
    run = AsyncMock()
    with patch("recipeparser.adapters.regen_worker.run_workers", new=run), \
         patch.object(api, "_get_supabase_service_client", return_value=None):
        with TestClient(api.app):
            pass
    run.assert_not_called()
