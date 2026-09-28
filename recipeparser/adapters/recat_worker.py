"""
recipeparser/adapters/recat_worker.py — bulk recategorise worker (spec 6).

A client-inserted ingestion_jobs row with kind = 'recategorize' and
params.category_ids = [...] asks for every recipe of that user to be checked
against ONLY those newly added tags.  Inserts are additive (on conflict do
nothing); nothing is ever removed.  Progress, cursor and cancellation all
live on the job row so PowerSync shows them and the client can stop it.

One ``run_once`` is one batch (Fix Roadmap F-008). ``run_workers`` awaits each
worker in turn, so a poll that ran a whole job held regeneration and shutdown
behind it for as long as the job took.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from recipeparser.core.stages.categorize import filter_batch_result
from recipeparser.core.taxonomy import root_of
from recipeparser.gemini import categorize_batch

log = logging.getLogger(__name__)

# recipes.id is a uuid column, so the paging cursor must always be a uuid.
# An empty string renders as `id=gt.` in PostgREST, which Postgres rejects with
# `invalid input syntax for type uuid: ""` — the nil uuid is the real floor.
NIL_UUID = "00000000-0000-0000-0000-000000000000"

# A `running` job whose row has not been touched for this long is assumed
# abandoned and is reclaimed. The worker writes `updated_at` after every batch,
# so the age of the row is a real measure of liveness rather than of job length;
# and `params.cursor` is written in the same statement, so a reclaimed job
# resumes from the last batch it finished instead of starting over. Since
# F-008 the gap between two writes is one batch plus the regen poll that runs
# between them, which must stay well inside this lease or a live job could be
# reclaimed by a second worker.
STALE_LEASE_MINUTES = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_new_axes(
    rows: List[Dict[str, Any]],
    category_ids: List[str],
) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """
    (axis -> [new tag names], tag name -> category id) for the ids in a job.
    A level-1 row (no parent) is an axis with a single tag of its own name,
    matching SupabaseCategorySource._build_axes.

    The axis of a tag is its ROOT, not its immediate parent (fixed 2026-09-13,
    stage 7C). Before that this read `parent_id` once, so on `Cuisine > Asian >
    Thai` ingest offered Thai under *Cuisine* while recategorise offered it
    under *Asian* — the same taxonomy described two ways, in prompts that are
    supposed to be interchangeable. `core.taxonomy.root_of` is now the single
    walk both paths use.

    Raises:
        ValueError: when two of the requested categories share a name (D6). The
            unique (user_id, name) index makes that unrepresentable today, so
            this cannot fire yet; it is here so that the day names are scoped
            per axis, a job misfiling every "Quick" recipe under the wrong axis
            fails loudly instead of last-write-wins picking a winner in silence.
    """
    by_id = {r["id"]: r for r in rows}
    axes: Dict[str, List[str]] = {}
    ids: Dict[str, str] = {}
    for cid in category_ids:
        row = by_id.get(cid)
        if row is None:
            continue
        name = (row.get("name") or "").strip()
        if not name:
            continue
        if name in ids and ids[name] != cid:
            raise ValueError(
                "Two categories are named {!r} ({} and {}): a tag name must identify "
                "one category, or a recategorise job files recipes under whichever "
                "one it happened to see last.".format(name, ids[name], cid)
            )
        root_id = root_of(cid, rows)
        root = by_id.get(root_id or "", {})
        axis = (root.get("name") or "").strip() or name
        axes.setdefault(axis, [])
        if name not in axes[axis]:
            axes[axis].append(name)
        ids[name] = cid
    return axes, ids


@dataclass
class _HeldJob:
    """
    A claimed job between polls. ``params`` carries the cursor, and it is written
    to the row after every batch, so a job whose worker dies is reclaimed (see
    STALE_LEASE_MINUTES) and resumes from its last finished batch. The rest is
    what the whole-job loop kept in its locals before F-008, carried between
    polls so the final done/error verdict is unchanged.
    """
    job_id: str
    user_id: str
    params: Dict[str, Any]
    new_axes: Dict[str, List[str]]
    tag_ids: Dict[str, str]
    total_batches: int
    skipped: List[Dict[str, Any]]
    skipped_count: int
    done_batches: int = 0
    failed: int = 0
    matched: int = 0
    offered: Set[str] = field(default_factory=set)


class RecatWorker:
    def __init__(
        self,
        supabase: Any,
        gemini_client: Any,
        *,
        categorize_fn: Callable[..., Dict[str, List[str]]] = categorize_batch,
        batch_size: int = 10,
    ) -> None:
        self._sb = supabase
        self._client = gemini_client
        self._categorize = categorize_fn
        self._batch_size = max(1, batch_size)
        self._held: Optional[_HeldJob] = None

    # ── one poll: at most one batch ─────────────────────────────────────

    def run_once(self) -> int:
        """
        Claim a job if none is held, then run ONE batch of it (or finish it).
        Returns 1 when it did anything, 0 when there was no job to work on.

        The job stays held in memory between polls; its row stays `running` with
        `updated_at` refreshed by every batch, so neither this worker's pending
        poll nor another worker's stale-lease reclaim will touch it meanwhile.
        """
        if self._held is None:
            job = self._claim()
            if job is None:
                return 0
            try:
                self._held = self._start(job)
            except Exception as exc:  # noqa: BLE001
                log.error("recat job %s crashed: %s", job["id"], exc, exc_info=True)
                self._finish(job["id"], "error", error=str(exc)[:2000])
                return 1
            if self._held is None:
                return 1                          # finished at the start, nothing to run
        held = self._held
        try:
            finished = self._step(held)
        except Exception as exc:  # noqa: BLE001
            # Let go first: if the terminal write fails too, the next poll must not
            # keep feeding batches to a job it has already given up on.
            self._held = None
            log.error("recat job %s crashed: %s", held.job_id, exc, exc_info=True)
            self._finish(held.job_id, "error", error=str(exc)[:2000])
            return 1
        if finished:
            self._held = None
        return 1

    def _claim(self) -> Optional[Dict[str, Any]]:
        jobs = (
            self._sb.table("ingestion_jobs").select("*")
            .eq("kind", "recategorize").eq("status", "pending")
            .order("created_at").limit(1).execute().data or []
        )
        prior_status = "pending"
        if not jobs:
            # Nothing waiting: look for a job a restart left `running`. Without this
            # such a job is never touched again -- it is not `pending`, so the poll
            # above skips it forever, and it sits at whatever progress it reached.
            jobs = self._stale_running_jobs()
            prior_status = "running"
        if not jobs:
            return None
        job = jobs[0]
        # Compare-and-swap on the status we read, so two workers racing for the same
        # row cannot both win it: the second update matches nothing.
        claimed = (
            self._sb.table("ingestion_jobs")
            .update({"status": "running", "stage": "CATEGORIZING", "updated_at": _now()})
            .eq("id", job["id"]).eq("status", prior_status).execute().data
        )
        return job if claimed else None

    # ── the job ──────────────────────────────────────────────────────────

    def _start(self, job: Dict[str, Any]) -> Optional[_HeldJob]:
        """Resolve the job's categories and size once, at claim. None if it finished here."""
        user_id = job["user_id"]
        params = dict(job.get("params") or {})
        cat_rows = self._sb.table("categories").select("id,name,parent_id").eq("user_id", user_id).execute().data or []
        requested = list(params.get("category_ids") or [])
        new_axes, tag_ids = resolve_new_axes(cat_rows, requested)
        if not tag_ids:
            # Every requested category has gone since the job was queued. Finishing
            # `done` here -- which is what this did until 2026-09-13 -- reads as
            # "checked your whole library, nothing matched", which is the opposite
            # of what happened: nothing was checked at all.
            self._finish(job["id"], "error", error="The categories no longer exist")
            return None
        missing = len(requested) - len(tag_ids)
        if missing:
            # Some resolved: proceed on those. The job is still worth running, and
            # the names that are gone are simply not offered to the model.
            log.warning(
                "recat job %s: %d of %d requested categories no longer exist; proceeding on the rest.",
                job["id"], missing, len(requested),
            )

        total = self._sb.table("recipes").select("id", count="exact").eq("user_id", user_id).execute().count or 0
        params["cursor"] = str(params.get("cursor") or NIL_UUID)
        return _HeldJob(
            job_id=job["id"],
            user_id=user_id,
            params=params,
            new_axes=new_axes,
            tag_ids=tag_ids,
            offered=set(tag_ids),
            total_batches=max(1, math.ceil(total / self._batch_size)),
            skipped=list(job.get("skipped") or []),
            skipped_count=int(job.get("skipped_count") or 0),
        )

    def _step(self, held: _HeldJob) -> bool:
        """Run the batch after ``params.cursor``, or finish the job. True when finished."""
        batch = (
            self._sb.table("recipes").select("id,title,ingredient_lines,direction_steps")
            .eq("user_id", held.user_id).gt("id", held.params["cursor"]).order("id")
            .limit(self._batch_size).execute().data or []
        )
        if not batch:
            if held.failed and held.failed > held.done_batches / 10:
                self._finish(held.job_id, "error", count=held.matched,
                             skipped=held.skipped, skipped_count=held.skipped_count,
                             error=f"{held.failed} of {held.done_batches} batches failed")
            else:
                self._finish(held.job_id, "done", progress=100, count=held.matched,
                             skipped=held.skipped, skipped_count=held.skipped_count)
            return True

        # Retried once, then recorded, then passed. A transient model or network
        # failure costs one retry; a batch that fails twice has its recipe ids
        # written to `skipped` with the reason BEFORE the cursor moves past them,
        # so "nothing was skipped" on the client means it. Until 2026-09-13 the
        # ids were dropped and only a counter survived.
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                raw = self._categorize(batch, held.new_axes, self._client)
                hits = filter_batch_result(raw, held.offered)
                rows = [
                    {"id": str(uuid.uuid4()), "recipe_id": rid, "category_id": held.tag_ids[tag],
                     "user_id": held.user_id}
                    for rid, tags in hits.items() for tag in tags
                ]
                if rows:
                    self._sb.table("recipe_categories").upsert(
                        rows, on_conflict="recipe_id,category_id", ignore_duplicates=True
                    ).execute()
                held.matched += len(hits)  # distinct recipes matched, not junction rows inserted
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 1:
                    log.info("recat job %s: batch failed (%s) — retrying once.", held.job_id, exc)
        if last_exc is not None:
            held.failed += 1
            reason = str(last_exc)[:500]
            held.skipped.extend({"recipe_id": r["id"], "reason": reason} for r in batch)
            held.skipped_count += len(batch)
            log.warning(
                "recat job %s: batch failed twice (%s) — %d recipe(s) recorded as skipped.",
                held.job_id, reason, len(batch),
            )
        held.done_batches += 1
        held.params["cursor"] = batch[-1]["id"]
        progress = min(99, int(100 * held.done_batches / held.total_batches))
        self._sb.table("ingestion_jobs").update({
            "params": held.params,
            "progress_pct": progress,
            "recipe_count": held.matched,
            "skipped": held.skipped,
            "skipped_count": held.skipped_count,
            "updated_at": _now(),
        }).eq("id", held.job_id).execute()
        status = (self._sb.table("ingestion_jobs").select("status").eq("id", held.job_id)
                  .limit(1).execute().data or [{}])[0].get("status")
        if status == "cancelled":
            # Finish it properly rather than returning. A bare return left the
            # row at stage CATEGORIZING with mid-flight progress, which is
            # indistinguishable from a worker that died; the screen has to be
            # able to say "Stopped. N recipes tagged so far".
            log.info("recat job %s cancelled after %d batch(es).", held.job_id, held.done_batches)
            self._finish(
                held.job_id, "cancelled", progress=progress, count=held.matched,
                skipped=held.skipped, skipped_count=held.skipped_count,
            )
            return True
        return False

    def _stale_running_jobs(self) -> List[Dict[str, Any]]:
        """A recategorise job left `running` past the lease, oldest first."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_LEASE_MINUTES)).isoformat()
        return (
            self._sb.table("ingestion_jobs").select("*")
            .eq("kind", "recategorize").eq("status", "running").lt("updated_at", cutoff)
            .order("created_at").limit(1).execute().data or []
        )

    def _finish(self, job_id: str, status: str, *, progress: Optional[int] = None,
                count: Optional[int] = None, error: Optional[str] = None,
                skipped: Optional[List[Dict[str, Any]]] = None,
                skipped_count: Optional[int] = None) -> None:
        payload: Dict[str, Any] = {
            "status": status,
            # `cancelled` is a finished job, not a failed one: a cook stopped it.
            # Only a real error gets stage ERROR, so a reader can tell them apart.
            "stage": "ERROR" if status == "error" else "DONE",
            "updated_at": _now(),
        }
        if progress is not None:
            payload["progress_pct"] = progress
        if count is not None:
            payload["recipe_count"] = count
        if skipped is not None:
            payload["skipped"] = skipped
        if skipped_count is not None:
            payload["skipped_count"] = skipped_count
        if error:
            payload["error_message"] = error
        self._sb.table("ingestion_jobs").update(payload).eq("id", job_id).execute()
