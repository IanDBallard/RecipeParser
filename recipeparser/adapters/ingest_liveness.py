"""Keep live ingest rows fresh, and end the ones whose process died (Cayenne F-001).

An ingest job exists only in the process that runs it (``api._active_jobs``). When that process
died mid-import — an OOM on the 2 GB VM, a forced deploy — its ``ingestion_jobs`` row stayed
``running`` for ever: every device adopted it and could not start another import, and deploy.sh
refused every later api deploy. Nothing ended it, because recategorise's reclaim resumes a job and
an ingest job cannot be resumed; its state died with the process.

Three parts, one loop:

- **Heartbeat.** Every tick touches ``updated_at`` on each job this process is running and still
  hearing from. Stage and progress writes alone leave gaps (a long OCR or extraction chunk), so
  without it "stale" could not be told from "slow".
- **Stall check** (Cayenne F-426). A job this process is running but has heard nothing from for
  ``STALL_MINUTES`` — no Gemini attempt, no back-off wait, no stage, no progress — is hung, not
  slow. The heartbeat used to keep such a job alive for ever; now the tick ends its row with
  ``STALLED_MESSAGE`` and cancels it. The job's ``JobSignal`` is what tells the two apart.
- **Reaper.** Every tick ends any ingest row still ``pending`` or ``running`` whose ``updated_at``
  is older than the lease. Because a live job on *any* instance keeps its own row fresh, a row
  only goes stale when the process that owned it is gone: that is a real restart, and only that
  case says so (``INTERRUPTED_MESSAGE``).

The tick also reads which of its own jobs' rows now say ``cancelled`` — a cancel another instance
took, the row being the one source of truth — and cancels those jobs here too.

Cayenne's ``deploy/lib/jobs.sh`` treats an ingest row as live only while it is inside this same
lease; the two values must move together.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Callable, Iterable, List, Mapping, Optional, Union

from recipeparser.core.clock import utc_timestamp
from recipeparser.core.job_signal import JobSignal

log = logging.getLogger(__name__)

# Five heartbeats' grace: one missed write is noise, five is a dead process.
LEASE_MINUTES = 5
TICK_SECONDS = 60
# A job that has done nothing for this long is hung. Well past the longest silence a working job
# keeps: one Gemini call (HTTP_TIMEOUT_SECS, 180 s) or one back-off wait (BACKOFF_MAX_SECS, 120 s).
STALL_MINUTES = 10

INTERRUPTED_MESSAGE = (
    "The import stopped when the server restarted. Recipes it had already added are in your "
    "library; add the source again for the rest."
)

STALLED_MESSAGE = (
    "The import stopped responding. Recipes it had already added are in your library; add the "
    "source again for the rest."
)

#: What the tick is told about this process's jobs: the ids alone, or each id with its signal
#: (None for a job that must never count as stalled, such as one the owner paused).
LiveJobs = Union[Iterable[str], Mapping[str, Optional[JobSignal]]]


def _stamp(now: datetime.datetime) -> str:
    return utc_timestamp(now)


def touch_live_jobs(sb: Any, job_ids: List[str], now: datetime.datetime) -> None:
    """Heartbeat: mark every job this process is running as alive. Changes nothing else."""
    if not job_ids:
        return
    sb.table("ingestion_jobs").update({"updated_at": _stamp(now)}).in_("id", job_ids).execute()


def cancelled_elsewhere(sb: Any, job_ids: List[str]) -> List[str]:
    """Which of these jobs' rows already say ``cancelled``."""
    if not job_ids:
        return []
    result = (
        sb.table("ingestion_jobs").select("id").in_("id", job_ids).eq("status", "cancelled").execute()
    )
    return [row["id"] for row in (result.data or [])]


def end_stalled_ingests(sb: Any, job_ids: List[str], now: datetime.datetime) -> None:
    """End this process's hung jobs' rows, unless they already finished."""
    if not job_ids:
        return
    (
        sb.table("ingestion_jobs")
        .update({
            "status": "error",
            "stage": "ERROR",
            "error_message": STALLED_MESSAGE,
            "updated_at": _stamp(now),
        })
        .in_("id", job_ids)
        .in_("status", ["pending", "running"])
        .execute()
    )


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


def tick(sb: Any, live_jobs: LiveJobs, now: datetime.datetime) -> None:
    """One stall check, one heartbeat, then one reap. Never raises: a failed tick must not end the loop."""
    try:
        jobs: Mapping[str, Optional[JobSignal]] = (
            dict(live_jobs) if isinstance(live_jobs, Mapping) else {job_id: None for job_id in live_jobs}
        )
        stalled = [
            job_id for job_id, signal in jobs.items()
            if signal is not None and not signal.cancelled and signal.idle_seconds() >= STALL_MINUTES * 60
        ]
        if stalled:
            end_stalled_ingests(sb, stalled, now)
            for job_id in stalled:
                log.warning("Ended ingest job %s: no progress for %d minutes.", job_id, STALL_MINUTES)
                signal = jobs[job_id]
                if signal is not None:
                    signal.cancel()  # if it ever wakes, it stops at its next checkpoint
        alive = [job_id for job_id in jobs if job_id not in stalled]
        # Touch first: reaping first could end this process's own job if its last write aged out.
        touch_live_jobs(sb, alive, now)
        watched = [job_id for job_id in alive if jobs[job_id] is not None and not jobs[job_id].cancelled]
        for job_id in cancelled_elsewhere(sb, watched):
            log.info("Ingest job %s: its row says cancelled — stopping it.", job_id)
            signal = jobs[job_id]
            if signal is not None:
                signal.cancel()
        for job_id in reap_stale_ingests(sb, now):
            log.warning(
                "Ended ingest job %s: no heartbeat for %d minutes — its process is gone.", job_id, LEASE_MINUTES
            )
    except Exception:
        log.exception("Ingest liveness tick failed; retrying next tick.")


async def run_liveness(
    sb: Any,
    live_jobs: Callable[[], LiveJobs],
    stop: asyncio.Event,
    tick_seconds: float = TICK_SECONDS,
) -> None:
    """Tick at once (a restart's own dead rows age out from here), then every ``tick_seconds``."""
    while not stop.is_set():
        now = datetime.datetime.now(datetime.timezone.utc)
        snapshot = live_jobs()
        snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else list(snapshot)
        await asyncio.to_thread(tick, sb, snapshot, now)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
