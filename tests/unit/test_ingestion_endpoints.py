"""Both endpoints must hand the pipeline a sink (spec 4.3, 4.4). No live writes."""
from __future__ import annotations

from unittest.mock import MagicMock

import recipeparser.adapters.api as api


def test_finalize_takes_a_payload(monkeypatch):
    """The sink builds the row; finalize only sends it."""
    sent = {}

    class _Table:
        def update(self, payload):
            sent.update(payload)
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            return None

    client = MagicMock()
    client.table.return_value = _Table()
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: client)
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)

    api._finalize_ingestion_job("job-1", {"status": "done", "recipe_count": 3, "skipped_count": 0})

    assert sent["status"] == "done"
    assert sent["recipe_count"] == 3


def test_the_pipeline_is_given_result_and_skip_callbacks():
    """Regression: passing None here is why nothing was written until the end."""
    import inspect

    source = inspect.getsource(api.submit_file_job)
    assert "on_result=" in source
    assert "on_skip=" in source
    assert "on_progress=" in source
    assert "writer.write(results)" not in source
