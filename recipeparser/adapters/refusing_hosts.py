"""The record of websites that refuse automated imports (Cayenne Library AI search, Stage 5).

``public.refusing_hosts`` is shared with Cayenne, service role only, one row per host with no
cook id: it says which sites refused a reader, not who asked. A refusal upserts the host; a
successful URL import from that host deletes it; Cayenne reads it to tell the cook which of
their sites to paste from. Every write is best effort: a failure is logged and swallowed, so
the record can never fail an import.

``sb`` is the service-role Supabase client. Callers get it from the API module's
``_get_supabase_service_client``, which is None in a test run (``live_writes_blocked``), so no
write reaches a real project from here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from recipeparser.core.citation import host_of

logger = logging.getLogger(__name__)

BLOCKED_DAYS = 30
_TABLE = "refusing_hosts"


def record_refusal(sb: Any, host: str) -> None:
    """Upsert ``host`` with last_refused_at = now; log and swallow any error. An empty host (an
    address without one) is not recorded."""
    if not host_of(host):
        return
    try:
        sb.table(_TABLE).upsert(
            {"host": host_of(host), "last_refused_at": datetime.now(timezone.utc).isoformat()}
        ).execute()
    except Exception:
        logger.exception("Could not record the refusal by %s.", host)


def clear_refusal(sb: Any, host: str) -> None:
    """Delete ``host``'s row; log and swallow any error."""
    try:
        sb.table(_TABLE).delete().eq("host", host_of(host)).execute()
    except Exception:
        logger.exception("Could not clear the refusal by %s.", host)


def blocked_hosts(sb: Any, hosts: Iterable[str], now: datetime) -> set[str]:
    """The normalised ``hosts`` that refused within the last BLOCKED_DAYS days."""
    wanted = {host_of(h) for h in hosts}
    if not wanted:
        return set()
    cutoff = (now - timedelta(days=BLOCKED_DAYS)).isoformat()
    rows = (
        sb.table(_TABLE).select("host").in_("host", sorted(wanted)).gte("last_refused_at", cutoff).execute().data
    )
    return {r["host"] for r in rows or []}
