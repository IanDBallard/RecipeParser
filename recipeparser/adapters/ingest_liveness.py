"""Keep live ingest rows fresh, and end the ones whose process died (Cayenne F-001).

An ingest job exists only in the process that runs it (``api._active_jobs``). When that process
died mid-import — an OOM on the 2 GB VM, a forced deploy — its ``ingestion_jobs`` row stayed
``running`` for ever: every device adopted it and could not start another import, and deploy.sh
refused every later api deploy. Nothing ended it, because recategorise's reclaim resumes a job and
an ingest job cannot be resumed; its state died with the process.

Two halves, one loop:

- **Heartbeat.** Every tick touches ``updated_at`` on each job this process is running. Stage and
  progress writes alone leave gaps (a long OCR or extraction chunk), so without it "stale" could
  not be told from "slow".
- **Reaper.** Every tick ends any ingest row still ``pending`` or ``running`` whose ``updated_at``
  is older than the lease. Because a live job on *any* instance keeps its own row fresh, this is
  safe when a second API points at the same project.

Cayenne's ``deploy/lib/jobs.sh`` treats an ingest row as live only while it is inside this same
lease; the two values must move together.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Callable, Iterable, List

from recipeparser.core.clock import utc_timestamp

log = logging.getLogger(__name__)

# Five heartbeats' grace: one missed write is noise, five is a dead process.
LEASE_MINUTES = 5
TICK_SECONDS = 60

INTERRUPTED_MESSAGE = (
    "The import stopped when the server restarted. Recipes it had already added are in your "
    "library; add the source again for the rest."
)


def _stamp(now: datetime.datetime) -> str:
    return utc_timestamp(now)


def touch_live_jobs(sb: Any, job_ids: List[str], now: datetime.datetime) -> None:
    """Heartbeat: mark every job this process is running as alive. Changes nothing else."""
    if not job_ids:
        return
    sb.table("ingestion_jobs").update({"updated_at": _stamp(now)}).in_("id", job_ids).execute()


def reap_stale_ingests(sb: Any, now: datetime.datetime) -> List[str]:
    """End every ingest row left unfinished past the lease; return the ids it ended."""
    cutoff = _stamp(now - datetime.timedelta(minutes=LEASE_MINUTES))
    result = (
        sb.table("ingestion_jobs")
        .update({
            "status": "error",
            "stage": "ERROR",
            "error_message": INTERRUPTED_MESSAGE,
            "updated_at": _stamp(now),
        })
        .eq("kind", "ingest")
        .in_("status", ["pending", "running"])
        .lt("updated_at", cutoff)
        .execute()
    )
    return [row["id"] for row in (result.data or [])]


def tick(sb: Any, live_ids: Iterable[str], now: datetime.datetime) -> None:
    """One heartbeat, then one reap. Never raises: a failed tick must not end the loop."""
    try:
        # Touch first: reaping first could end this process's own job if its last write aged out.
        touch_live_jobs(sb, list(live_ids), now)
        for job_id in reap_stale_ingests(sb, now):
            log.warning("Ended ingest job %s: no heartbeat for %d minutes.", job_id, LEASE_MINUTES)
    except Exception:
        log.exception("Ingest liveness tick failed; retrying next tick.")


async def run_liveness(
    sb: Any,
    live_ids: Callable[[], Iterable[str]],
    stop: asyncio.Event,
    tick_seconds: float = TICK_SECONDS,
) -> None:
    """Tick at once (a restart's own dead rows age out from here), then every ``tick_seconds``."""
    while not stop.is_set():
        now = datetime.datetime.now(datetime.timezone.utc)
        await asyncio.to_thread(tick, sb, list(live_ids()), now)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
