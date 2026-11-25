"""cleanup_jobs.py never reports a job as updated when Supabase refused the write."""
from __future__ import annotations

import httpx
import pytest

import cleanup_jobs

_URL = "https://x.supabase.co"


def _response(method: str, status: int, body=None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {}, request=httpx.Request(method, _URL))


def test_a_refused_update_is_reported_and_counted(monkeypatch, capsys):
    monkeypatch.setattr(cleanup_jobs.httpx, "get", lambda url, headers: _response("GET", 200, [{"id": "a"}, {"id": "b"}]))
    answers = {"a": 200, "b": 403}
    monkeypatch.setattr(
        cleanup_jobs.httpx, "patch",
        lambda url, headers, json: _response("PATCH", answers[url.rsplit(".", 1)[-1]], {"message": "denied"}),
    )

    assert cleanup_jobs.cleanup(_URL, "k") == 1
    out = capsys.readouterr().out
    assert "Job a updated to error." in out
    assert "Job b updated to error." not in out
    assert "Job b NOT updated: HTTP 403" in out


def test_a_refused_fetch_fails_the_run(monkeypatch):
    monkeypatch.setattr(cleanup_jobs.httpx, "get", lambda url, headers: _response("GET", 401, {"message": "bad key"}))
    with pytest.raises(httpx.HTTPStatusError):
        cleanup_jobs.cleanup(_URL, "k")


def test_main_exits_1_when_any_update_was_refused(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", _URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(cleanup_jobs, "cleanup", lambda url, key: 2)
    assert cleanup_jobs.main() == 1
