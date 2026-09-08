"""
recipeparser/adapters/recat_worker.py — bulk recategorise worker (spec 6).

A client-inserted ingestion_jobs row with kind = 'recategorize' and
params.category_ids = [...] asks for every recipe of that user to be checked
against ONLY those newly added tags.  Inserts are additive (on conflict do
nothing); nothing is ever removed.  Progress, cursor and cancellation all
live on the job row so PowerSync shows them and the client can stop it.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from recipeparser.core.stages.categorize import chunked, filter_batch_result
from recipeparser.gemini import categorize_batch

log = logging.getLogger(__name__)


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
    """
    by_id = {r["id"]: r for r in rows}
    axes: Dict[str, List[str]] = {}
    ids: Dict[str, str] = {}
    for cid in category_ids:
        row = by_id.get(cid)
        if row is None:
            continue
        name = (row.get("name") or "").strip()
        parent = by_id.get(row.get("parent_id") or "")
        axis = (parent.get("name") or "").strip() if parent else name
        axes.setdefault(axis, [])
        if name not in axes[axis]:
            axes[axis].append(name)
        ids[name] = cid
    return axes, ids


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

    # ── one poll: at most one job ───────────────────────────────────────

    def run_once(self) -> int:
        jobs = (
            self._sb.table("ingestion_jobs").select("*")
            .eq("kind", "recategorize").eq("status", "pending")
            .order("created_at").limit(1).execute().data or []
        )
        if not jobs:
            return 0
        job = jobs[0]
        claimed = (
            self._sb.table("ingestion_jobs")
            .update({"status": "running", "stage": "CATEGORIZING", "updated_at": _now()})
            .eq("id", job["id"]).eq("status", "pending").execute().data
        )
        if not claimed:
            return 0
        try:
            self._run_job(job)
        except Exception as exc:  # noqa: BLE001
            log.error("recat job %s crashed: %s", job["id"], exc, exc_info=True)
            self._finish(job["id"], "error", error=str(exc)[:2000])
        return 1

    # ── the job ──────────────────────────────────────────────────────────

    def _run_job(self, job: Dict[str, Any]) -> None:
        user_id = job["user_id"]
        params = dict(job.get("params") or {})
        cat_rows = self._sb.table("categories").select("id,name,parent_id").eq("user_id", user_id).execute().data or []
        new_axes, tag_ids = resolve_new_axes(cat_rows, list(params.get("category_ids") or []))
        offered = set(tag_ids)
        if not offered:
            self._finish(job["id"], "done", progress=100, count=0)
            return

        total = self._sb.table("recipes").select("id", count="exact").eq("user_id", user_id).execute().count or 0
        total_batches = max(1, math.ceil(total / self._batch_size))
        cursor = str(params.get("cursor") or "")
        done_batches = failed = matched = 0

        while True:
            page = (
                self._sb.table("recipes").select("id,title,ingredient_lines,direction_steps")
                .eq("user_id", user_id).gt("id", cursor).order("id").limit(self._batch_size)
                .execute().data or []
            )
            if not page:
                break
            for batch in chunked(page, self._batch_size):
                try:
                    raw = self._categorize(batch, new_axes, self._client)
                    hits = filter_batch_result(raw, offered)
                    rows = [
                        {"id": str(uuid.uuid4()), "recipe_id": rid, "category_id": tag_ids[tag], "user_id": user_id}
                        for rid, tags in hits.items() for tag in tags
                    ]
                    if rows:
                        self._sb.table("recipe_categories").upsert(
                            rows, on_conflict="recipe_id,category_id", ignore_duplicates=True
                        ).execute()
                    matched += len(rows)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    log.warning("recat job %s: batch failed (%s) — skipped.", job["id"], exc)
                done_batches += 1
                cursor = batch[-1]["id"]
                params["cursor"] = cursor
                self._sb.table("ingestion_jobs").update({
                    "params": params,
                    "progress_pct": min(99, int(100 * done_batches / total_batches)),
                    "recipe_count": matched,
                    "updated_at": _now(),
                }).eq("id", job["id"]).execute()
                status = (self._sb.table("ingestion_jobs").select("status").eq("id", job["id"])
                          .limit(1).execute().data or [{}])[0].get("status")
                if status == "cancelled":
                    log.info("recat job %s cancelled after %d batch(es).", job["id"], done_batches)
                    return

        if failed and failed > done_batches / 10:
            self._finish(job["id"], "error", count=matched,
                         error=f"{failed} of {done_batches} batches failed")
        else:
            self._finish(job["id"], "done", progress=100, count=matched)

    def _finish(self, job_id: str, status: str, *, progress: Optional[int] = None,
                count: Optional[int] = None, error: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {
            "status": status,
            "stage": "DONE" if status == "done" else "ERROR",
            "updated_at": _now(),
        }
        if progress is not None:
            payload["progress_pct"] = progress
        if count is not None:
            payload["recipe_count"] = count
        if error:
            payload["error_message"] = error
        self._sb.table("ingestion_jobs").update(payload).eq("id", job_id).execute()
