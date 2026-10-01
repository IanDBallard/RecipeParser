"""
recipeparser/adapters/share_worker.py — the recipe-sharing copy job and sweep (Cayenne's
recipe-sharing design, Parts 3 and 4).

Accepting a share queues an ingestion_jobs row with kind = 'share_accept' and
params.share_id. This worker claims it the way RecatWorker claims a recategorise job, then
copies ONE item per poll: the picture first (stored again under the copy's own id, so a
picture the sender later replaces or removes is not the recipient's), then the database
function copy_shared_item, which writes the recipe, its categories, the item and the share's
count in one transaction. The copy's id is uuid5(SHARE_NAMESPACE, item id), so a redo after
a crash lands on the same row and the same object key.

The same worker runs the sweep: pending shares past expires_at become expired every fifteen
minutes, and shares resolved more than thirty days ago are deleted once a day.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from recipeparser.adapters.recat_worker import STALE_LEASE_MINUTES
from recipeparser.core.clock import utc_timestamp
from recipeparser.io.writers.image_store import BUCKET

log = logging.getLogger(__name__)

# Fixed for ever: changing it would give a redo a different id from the first attempt, and
# the copy would be made twice.
SHARE_NAMESPACE = uuid.UUID("fdcd01df-db30-423b-91de-7586d4ec7ea2")

EXPIRE_EVERY_SECONDS = 15 * 60
PURGE_EVERY_SECONDS = 24 * 60 * 60

_OPEN = ["pending", "copying"]
_SETTLED = {"accepted", "duplicate", "unavailable", "failed"}
_TYPE_BY_EXTENSION = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
}


def _now() -> str:
    return utc_timestamp()


def copy_id(item_id: str) -> str:
    """The recipient's recipe id for a share item: the same on every attempt (Part 3, step 4)."""
    return str(uuid.uuid5(SHARE_NAMESPACE, item_id))


def bucket_key(image_url: Optional[str], supabase_url: str) -> Optional[str]:
    """The object key of a picture in the recipe-images bucket, or None for any other URL.

    Pictures carry a ``?v=`` stamp (api._versioned), which is not part of the key.
    """
    if not image_url or not supabase_url:
        return None
    prefix = f"{supabase_url.rstrip('/')}/storage/v1/object/public/{BUCKET}/"
    bare = image_url.split("?", 1)[0]
    if not bare.startswith(prefix):
        return None
    return bare[len(prefix):] or None


@dataclass
class _HeldJob:
    job_id: str
    share_id: str
    sender_id: str
    recipient_id: str
    total: int
    settled: int


class ShareWorker:
    # One poll is one item; a held job should move item to item rather than wait a poll
    # interval between them (the same reasoning as RecatWorker, Fix Roadmap F-008).
    keeps_loop_busy = True

    def __init__(
        self,
        supabase: Any,
        image_store: Any,
        *,
        supabase_url: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sb = supabase
        self._images = image_store
        self._supabase_url = supabase_url if supabase_url is not None else os.environ.get("SUPABASE_URL", "")
        self._clock = clock
        self._held: Optional[_HeldJob] = None
        self._next_expire: Optional[float] = None
        self._next_purge: Optional[float] = None

    # ── one poll: the sweep when due, then at most one item ─────────────

    def run_once(self) -> int:
        """Returns 1 when it worked on a job, 0 when there was none. The sweep does not count:
        it must not keep run_workers from sleeping."""
        self._sweep()
        # A failure below is the database or the network, not an item (an item's own failure
        # is caught in _step), and it is raised, never turned into an `error` job: that would
        # strand the share in `accepting` for good, while the copy's fixed ids make a redo
        # harmless. Raising is what run_workers counts as an idle poll, so an outage cannot
        # spin the loop, and /health's stamp for this worker stops advancing, so it shows.
        if self._held is None:
            job = self._claim()
            if job is None:
                return 0
            try:
                self._held = self._start(job)
            except Exception:
                # Never held: the job stays `running`, and its lease brings it back.
                log.error("share job %s: could not start; the lease will retry it.", job["id"])
                raise
            if self._held is None:
                return 1
        try:
            finished = self._step(self._held)
        except Exception:
            # Still ours, so keep holding it: the next poll, after run_workers' sleep, carries
            # on from here. Letting go would idle the share until the lease lapsed, ten
            # minutes of "Copying" over a one-second blip.
            log.error("share job %s: poll failed; retrying on the next poll.", self._held.job_id)
            raise
        if finished:
            self._held = None
        return 1

    def _claim(self) -> Optional[Dict[str, Any]]:
        jobs = (
            self._sb.table("ingestion_jobs").select("*")
            .eq("kind", "share_accept").eq("status", "pending")
            .order("created_at").limit(1).execute().data or []
        )
        prior_status = "pending"
        if not jobs:
            jobs = self._stale_running_jobs()
            prior_status = "running"
        if not jobs:
            return None
        job = jobs[0]
        # `stage` is left as queued (IDLE): ingestion_jobs' stage check has no copying value,
        # and no screen reads a share job's stage — the client follows the items instead.
        claim = (
            self._sb.table("ingestion_jobs")
            .update({"status": "running", "updated_at": _now()})
            .eq("id", job["id"]).eq("status", prior_status)
        )
        if prior_status == "running":
            # RecatWorker's reclaim guard (Fix Roadmap F-112): only one of two workers that
            # read the same stale row matches the updated_at it read.
            claim = claim.eq("updated_at", job["updated_at"])
        return job if claim.execute().data else None

    def _stale_running_jobs(self) -> List[Dict[str, Any]]:
        cutoff = utc_timestamp(datetime.now(timezone.utc) - timedelta(minutes=STALE_LEASE_MINUTES))
        return (
            self._sb.table("ingestion_jobs").select("*")
            .eq("kind", "share_accept").eq("status", "running").lt("updated_at", cutoff)
            .order("created_at").limit(1).execute().data or []
        )

    # ── the job ──────────────────────────────────────────────────────────

    def _start(self, job: Dict[str, Any]) -> Optional[_HeldJob]:
        share_id = str((job.get("params") or {}).get("share_id") or "")
        shares = (
            self._sb.table("recipe_shares").select("id,sender_id,recipient_id,status")
            .eq("id", share_id).limit(1).execute().data or []
        ) if share_id else []
        if not shares or shares[0]["recipient_id"] != job["user_id"]:
            self._finish(job["id"], "error", error="The share no longer exists")
            return None
        share = shares[0]
        if share.get("status") != "accepting":
            # Only accept_recipe_share moves a share to `accepting`, in the transaction that queues
            # its job. A job for a share in any other state was not made by an accept, so copying
            # it would hand over recipes the sender cancelled or the recipient declined.
            self._finish(job["id"], "error", error="The share is not being accepted")
            return None
        items = (self._sb.table("recipe_share_items").select("id,status")
                 .eq("share_id", share_id).execute().data or [])
        taken = [i for i in items if i["status"] != "skipped"]
        return _HeldJob(
            job_id=job["id"], share_id=share_id,
            sender_id=share["sender_id"], recipient_id=share["recipient_id"],
            total=max(1, len(taken)),
            settled=sum(1 for i in taken if i["status"] in _SETTLED),
        )

    def _step(self, held: _HeldJob) -> bool:
        """Copy the next item, or finish the share and the job. True when finished."""
        items = (
            self._sb.table("recipe_share_items").select("id,source_recipe_id,status")
            .eq("share_id", held.share_id).in_("status", _OPEN)
            .order("position").limit(1).execute().data or []
        )
        if not items:
            self._sb.rpc("finish_recipe_share", {"p_share": held.share_id}).execute()
            share = (self._sb.table("recipe_shares").select("accepted_count")
                     .eq("id", held.share_id).limit(1).execute().data or [{}])[0]
            self._finish(held.job_id, "done", progress=100, count=int(share.get("accepted_count") or 0))
            return True

        item = items[0]
        (self._sb.table("recipe_share_items").update({"status": "copying"})
         .eq("id", item["id"]).in_("status", _OPEN).execute())
        try:
            outcome = self._copy(held, item)
        except Exception as exc:  # noqa: BLE001
            # Part 3, step 7: this item fails and the rest go on.
            log.warning("share %s: item %s failed (%s).", held.share_id, item["id"], exc)
            (self._sb.table("recipe_share_items").update({"status": "failed"})
             .eq("id", item["id"]).in_("status", _OPEN).execute())
            outcome = "failed"
        held.settled += 1
        log.info("share %s: item %s %s.", held.share_id, item["id"], outcome)
        self._sb.table("ingestion_jobs").update({
            "progress_pct": min(99, int(100 * held.settled / held.total)),
            "updated_at": _now(),
        }).eq("id", held.job_id).execute()
        return False

    def _copy(self, held: _HeldJob, item: Dict[str, Any]) -> str:
        new_id = copy_id(item["id"])
        image_url, image_source, stored = self._picture(held, item, new_id)
        params = {"p_item": item["id"], "p_new_id": new_id,
                  "p_image_url": image_url, "p_image_source": image_source}
        # A call that raised may still have committed, its reply lost: the picture stays then,
        # since removing it could leave the new recipe pointing at nothing, and an unused
        # object costs less than that.
        try:
            outcome = str(self._sb.rpc("copy_shared_item", params).execute().data)
        except Exception as exc:  # noqa: BLE001
            # Once more, as RecatWorker retries a batch: a dropped connection should not cost a
            # recipe. The function is idempotent, so a retry after a commit is harmless.
            log.info("share %s: copying item %s failed (%s) — retrying once.", held.share_id, item["id"], exc)
            outcome = str(self._sb.rpc("copy_shared_item", params).execute().data)
        if stored and outcome != "accepted":
            # The original went, or another share's copy won the race: no recipe points at
            # the picture just stored, and it would be paid for for ever.
            self._discard(new_id)
        return outcome

    def _discard(self, new_id: str) -> None:
        try:
            self._images.remove(new_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not remove the unused picture stored for %s (%s).", new_id, exc)

    def _picture(
        self, held: _HeldJob, item: Dict[str, Any], new_id: str,
    ) -> Tuple[Optional[str], Optional[str], bool]:
        """(image_url, image_source, stored) for the copy (Part 3, step 5).

        A bucket picture is read and stored again under ``new_id``, and ``stored`` says an
        object was written; any other URL is copied as it is. If reading or storing fails the
        copy goes ahead without a picture: a missing picture is a smaller loss than a missing
        recipe.
        """
        received = (
            self._sb.table("recipes").select("id").eq("user_id", held.recipient_id)
            .eq("copied_from_recipe_id", item["source_recipe_id"]).limit(1).execute().data or []
        )
        if received:
            # Already in the recipient's library, from this attempt or an earlier share: the
            # function makes no new row, so a stored picture would be an orphan.
            return None, None, False
        originals = (
            self._sb.table("recipes").select("image_url,image_source")
            .eq("id", item["source_recipe_id"]).eq("user_id", held.sender_id).limit(1).execute().data or []
        )
        if not originals or not originals[0].get("image_url"):
            return None, None, False
        url, source = originals[0]["image_url"], originals[0].get("image_source")
        key = bucket_key(url, self._supabase_url)
        if key is None:
            return url, source, False
        try:
            data = self._sb.storage.from_(BUCKET).download(key)
            content_type = _TYPE_BY_EXTENSION.get(key.rsplit(".", 1)[-1].lower(), "image/jpeg")
            stored = self._images.put(data, new_id, content_type)
        except Exception as exc:  # noqa: BLE001
            log.warning("share %s: could not copy the picture of item %s (%s); copying without it.",
                        held.share_id, item["id"], exc)
            stored = None
        return (stored, source, True) if stored else (None, None, False)

    def _finish(self, job_id: str, status: str, *, progress: Optional[int] = None,
                count: Optional[int] = None, error: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {
            "status": status,
            "stage": "ERROR" if status == "error" else "DONE",
            "updated_at": _now(),
        }
        if progress is not None:
            payload["progress_pct"] = progress
        if count is not None:
            payload["recipe_count"] = count
        if error:
            payload["error_message"] = error
        self._sb.table("ingestion_jobs").update(payload).eq("id", job_id).eq("status", "running").execute()

    # ── the sweep (Part 4) ───────────────────────────────────────────────

    def _sweep(self) -> None:
        now = self._clock()
        if self._next_expire is None or now >= self._next_expire:
            self._next_expire = now + EXPIRE_EVERY_SECONDS
            self._sweep_call("expire_recipe_shares")
        if self._next_purge is None or now >= self._next_purge:
            self._next_purge = now + PURGE_EVERY_SECONDS
            self._sweep_call("purge_recipe_shares")

    def _sweep_call(self, fn: str) -> None:
        """Best effort: a failure waits for the next due time rather than retrying every poll,
        which would fill the log for as long as the database is away."""
        try:
            n = self._sb.rpc(fn, {}).execute().data
        except Exception as exc:  # noqa: BLE001
            log.error("%s failed: %s", fn, exc)
            return
        if n:
            log.info("%s: %s share(s).", fn, n)
