"""Ingestion takes uom/measure from profiles so it agrees with the worker (spec 5.7)."""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DISABLE_AUTH", "1")
os.environ.setdefault("TEST_USER_ID", "00000000-0000-4000-8000-000000000001")

import recipeparser.adapters.api as api  # noqa: E402


def _sb(rows):
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.limit.return_value = q
    q.execute.return_value = MagicMock(data=rows)
    sb = MagicMock()
    sb.table.return_value = q
    return sb


def test_profile_row_wins():
    with patch.object(api, "_get_supabase_service_client",
                      return_value=_sb([{"uom_system": "Metric", "measure_preference": "Weight"}])):
        assert api._resolve_prefs("u1", "US", "Volume") == ("Metric", "Weight")


def test_body_is_fallback_without_row():
    with patch.object(api, "_get_supabase_service_client", return_value=_sb([])):
        assert api._resolve_prefs("u1", "Imperial", "Weight") == ("Imperial", "Weight")


def test_body_is_fallback_without_client():
    with patch.object(api, "_get_supabase_service_client", return_value=None):
        assert api._resolve_prefs("u1", "Imperial", "Weight") == ("Imperial", "Weight")
