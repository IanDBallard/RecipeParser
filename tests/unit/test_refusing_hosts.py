"""The refusal record, its use by the import job, and source_url on POST /jobs. Fake client only."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, List, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import recipeparser.adapters.api as api
from recipeparser.adapters import refusing_hosts
from recipeparser.core.fsm import PipelineController
from recipeparser.exceptions import SiteRefusedError

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


class FakeTable:
    def __init__(self, rows: List[dict]):
        self.rows, self.calls = rows, []
        self._cutoff = None
        self._hosts = None

    def upsert(self, row):
        self.calls.append(("upsert", row))
        return self

    def delete(self):
        self.calls.append(("delete",))
        return self

    def eq(self, col, val):
        self.calls.append(("eq", col, val))
        return self

    def select(self, _cols):
        return self

    def in_(self, _col, hosts):
        self._hosts = hosts
        return self

    def gte(self, _col, cutoff):
        self._cutoff = cutoff
        return self

    def execute(self):
        out = MagicMock()
        out.data = [r for r in self.rows if r["host"] in self._hosts and r["last_refused_at"] >= self._cutoff]
        return out


class FakeSb:
    def __init__(self, rows=()):
        self.t = FakeTable(list(rows))

    def table(self, name):
        assert name == "refusing_hosts"
        return self.t


def test_blocked_hosts_window():
    sb = FakeSb([
        {"host": "a.com", "last_refused_at": (NOW - timedelta(days=29)).isoformat()},
        {"host": "b.com", "last_refused_at": (NOW - timedelta(days=31)).isoformat()},
    ])
    assert refusing_hosts.blocked_hosts(sb, ["https://www.a.com/x", "b.com"], NOW) == {"a.com"}


def test_record_and_clear_normalise_the_host():
    sb = FakeSb()
    refusing_hosts.record_refusal(sb, "WWW.NYTimes.com")
    refusing_hosts.clear_refusal(sb, "https://www.food52.com/x")
    assert sb.t.calls[0][1]["host"] == "nytimes.com"
    assert ("eq", "host", "food52.com") in sb.t.calls


def test_record_refusal_swallows_errors():
    sb = MagicMock()
    sb.table.side_effect = RuntimeError("db down")
    refusing_hosts.record_refusal(sb, "nytimes.com")
    refusing_hosts.clear_refusal(sb, "nytimes.com")


def _run(monkeypatch, read, url_host) -> List[Tuple[str, bool]]:
    notes: List[Tuple[str, bool]] = []
    monkeypatch.setattr(api, "_note_refusal", lambda host, refused: notes.append((host, refused)))
    monkeypatch.setattr(api, "_finalize_ingestion_job", lambda jid, payload: None)
    monkeypatch.setattr(api, "_update_total_chunks", lambda *a, **k: None)
    monkeypatch.setattr(api, "_make_progress_writer", lambda jid: (lambda pct: None))
    monkeypatch.setattr(api, "_create_restore_categories", lambda *a, **k: {})
    monkeypatch.setattr(api._ai_gate, "allowance", lambda uid: None)
    monkeypatch.setattr(api.usage, "attributed", lambda *a, **k: nullcontext())
    with patch.object(api, "RecipePipeline"), patch.object(api, "SupabaseCategorySource") as cat, \
            patch.object(api, "SupabaseImageStore"):
        cat.return_value.load_category_ids.return_value = {}
        asyncio.run(api._run_ingestion_job("j", "u", PipelineController(), read, None, "Job", url_host=url_host))
    return notes


def test_refusal_recorded_on_site_refused(monkeypatch):
    async def read():
        raise SiteRefusedError("refused", host="nytimes.com")

    assert _run(monkeypatch, read, "nytimes.com") == [("nytimes.com", True)]


def test_refusal_cleared_on_url_success(monkeypatch):
    async def read():
        return MagicMock(), []

    assert _run(monkeypatch, read, "food52.com") == [("food52.com", False)]


def test_text_job_with_source_url_does_not_clear(monkeypatch):
    async def read():
        return MagicMock(), []

    assert _run(monkeypatch, read, None) == []


def test_note_refusal_skips_without_a_client(monkeypatch):
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: None)
    api._note_refusal("x.com", True)  # no client, no write, no raise


def test_note_refusal_writes_through_the_fake(monkeypatch):
    sb = FakeSb()
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: sb)
    api._note_refusal("x.com", True)
    api._note_refusal("y.com", False)
    assert sb.t.calls[0][0] == "upsert" and ("eq", "host", "y.com") in sb.t.calls


@pytest.fixture
def client(monkeypatch):
    api.app.dependency_overrides[api._verify_supabase_jwt] = lambda: {"sub": "u1"}
    api.app.dependency_overrides[api._require_ai_import] = lambda: None
    api._idempotency.reset()
    monkeypatch.setattr(api, "_create_ingestion_job", lambda *a, **k: None)
    yield TestClient(api.app, raise_server_exceptions=False)
    api.app.dependency_overrides.clear()
    api._active_jobs.clear()


def test_jobs_source_url_sets_chunk(client, monkeypatch):
    captured: List[Any] = []

    async def fake_run(job_id, user_id, controller, read, *a, **k):
        captured.append((await read(), k))

    fetched = MagicMock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(api, "UrlReader", lambda: MagicMock(read=fetched))
    monkeypatch.setattr(api, "_fetch_page_meta", fetched)
    monkeypatch.setattr(api, "_get_client", lambda: MagicMock())
    monkeypatch.setattr(api, "_run_ingestion_job", fake_run)

    async def go():
        resp = await asyncio.to_thread(
            client.post, "/jobs", json={"text": "Boil.", "source_url": "https://cooking.nytimes.com/r/1"}
        )
        await asyncio.sleep(0.05)
        return resp

    resp = asyncio.run(go())
    assert resp.status_code == 202
    (_client, chunks), kwargs = captured[0]
    assert chunks[0].source_url == "https://cooking.nytimes.com/r/1"
    assert chunks[0].citation is not None and chunks[0].citation.key == "cooking.nytimes.com"
    assert kwargs.get("url_host") is None
    fetched.assert_not_called()


@pytest.mark.parametrize("body", [
    {"text": "x", "source_url": "http://a.com/r"},
    {"source_url": "https://a.com/r"},
    {"url": "https://a.com/r", "source_url": "https://a.com/r"},
    {"text": "x", "source_url": "https://a.com/" + "a" * 2000},
])
def test_jobs_source_url_rejected(client, body):
    assert client.post("/jobs", json=body).status_code == 400
