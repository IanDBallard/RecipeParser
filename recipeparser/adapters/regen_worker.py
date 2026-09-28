"""
recipeparser/adapters/regen_worker.py — the reprocess worker (spec 5).

Stale rows (derived_rev < body_rev) are the queue.  Each poll claims up to
``batch`` rows via the claim_stale_recipes RPC, runs REFINE then EMBED, and
writes the derived columns back guarded by body_rev.  A run that raises is
recorded through the regen_failed RPC.  This is the only module that knows
the table and RPC names.  The claim RPC returns source_url (Cayenne migration
verbatim_ingestion) so REFINE sees the host.
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence

from recipeparser.core.citation import host_of
from recipeparser.core.regen import build_extraction, build_update
from recipeparser.core.stages.embed import embed
from recipeparser.core.stages.refine import refine
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource

log = logging.getLogger(__name__)

# claim_stale_recipes treats a claim older than five minutes as abandoned and
# hands the row to the next poll (Cayenne migration 014). The two must agree.
CLAIM_LEASE_SECONDS = 300.0
# How long before the lease runs out this worker stops treating a claim as its
# own. It covers the release request's own round trip, so the release reaches
# the database while the lease still holds.
CLAIM_RELEASE_MARGIN_SECONDS = 60.0


class RegenWorker:
    def __init__(
        self,
        supabase: Any,
        gemini_client: Any,
        *,
        refine_fn: Callable[..., Any] = refine,
        embed_fn: Callable[..., List[float]] = embed,
        axes_loader: Optional[Callable[[str], Dict[str, List[str]]]] = None,
        batch: int = 5,
        concurrency: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sb = supabase
        self._client = gemini_client
        self._refine = refine_fn
        self._embed = embed_fn
        self._axes = axes_loader or (lambda uid: SupabaseCategorySource().load_axes(uid))
        self._batch = max(1, batch)
        self._concurrency = max(1, concurrency)
        self._clock = clock

    # ── one poll ─────────────────────────────────────────────────────────

    def run_once(self) -> int:
        # Read before the claim is asked for, so the elapsed time measured from it
        # is never shorter than the age of the claim (see _release_claim).
        claimed_at = self._clock()
        rows = self._sb.rpc("claim_stale_recipes", {"p_limit": self._batch}).execute().data or []
        if not rows:
            return 0
        log.info("regen: claimed %d stale recipe(s).", len(rows))
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            list(pool.map(lambda row: self._process(row, claimed_at), rows))
        return len(rows)

    # ── one recipe ───────────────────────────────────────────────────────

    def _process(self, row: Dict[str, Any], claimed_at: float) -> None:
        # The unpack lives inside the try: a claimed row missing id or body_rev
        # would otherwise raise straight out of pool.map(), out of run_once(),
        # and abandon every other row in the batch. rid/read_rev are seeded for
        # the log so a malformed row is still identifiable.
        rid, read_rev = row.get("id", "?"), -1
        try:
            rid, read_rev = row["id"], int(row["body_rev"])
            refinement = self._refine(
                build_extraction(row),
                self._client,
                source_host=host_of(row["source_url"]) if row.get("source_url") else None,
                user_axes=self._axes(row["user_id"]),
            )
            vector = self._embed(recipe=refinement, client=self._client)
            payload = build_update(refinement, vector, read_rev)
            res = (
                self._sb.table("recipes")
                .update(payload)
                .eq("id", rid)
                .eq("body_rev", read_rev)
                .execute()
            )
            if not res.data:
                log.info("regen: %s was edited again during the run (rev %d) — result dropped.",
                         rid, read_rev)
                self._release_claim(rid, claimed_at)
            else:
                log.info("regen: %s regenerated at rev %d.", rid, read_rev)
        except Exception as exc:  # noqa: BLE001 — every failure is recorded, never fatal
            log.warning("regen: %s failed at rev %d: %s", rid, read_rev, exc, exc_info=True)
            self._record_failure(rid, exc)

    def _release_claim(self, rid: str, claimed_at: float) -> None:
        """
        Clear claimed_at on a row whose result was dropped, never raising.

        The cook edited the recipe mid-run, so its new revision is already stale
        and waiting — but it also still carried this run's claim, and so sat out
        the whole five-minute lease before anyone regenerated it (Fix Roadmap
        F-006). Only a successful write-back or regen_failed cleared the claim.

        The release is safe only while the claim is certainly still ours. Nobody
        else can claim the row until the lease runs out, so inside the lease the
        claimed_at on the row is this run's and clearing it clobbers nothing.
        Past that point another worker may have claimed the new revision, and
        clearing ITS claim would let a third run start alongside it. Elapsed time
        is measured on this process's monotonic clock from before the claim RPC
        was sent — longer than the claim's true age — so database clock skew
        cannot make a late release look early. Once the lease may have gone, the
        row is claimable again anyway, so leaving it costs nothing.

        A release that fails is logged, not sent to regen_failed: that RPC counts
        an attempt against the row's current body_rev, the new revision, which
        has not failed.
        """
        elapsed = self._clock() - claimed_at
        if elapsed >= CLAIM_LEASE_SECONDS - CLAIM_RELEASE_MARGIN_SECONDS:
            log.info("regen: %s's claim is %.0f s old — left to its lease rather than released.",
                     rid, elapsed)
            return
        try:
            self._sb.table("recipes").update({"claimed_at": None}).eq("id", rid).execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("regen: could not release the claim on %s (%s) — it waits out its lease.",
                        rid, exc, exc_info=True)

    def _record_failure(self, rid: str, exc: BaseException) -> None:
        """
        Record one failed attempt, never raising.

        regen_failed can fail under exactly the conditions that made the primary
        call fail — RPC missing, Supabase down. Letting that escape _process
        re-raises it out of ``list(pool.map(...))`` and out of ``run_once()``,
        and the row then keeps its claimed_at and gains no derived_attempts: it
        is re-claimed every five minutes forever, burning a REFINE and an EMBED
        call each time. That is a cost loop, not a lost error message.
        """
        try:
            self._sb.rpc("regen_failed", {"p_id": rid, "p_msg": str(exc)[:2000]}).execute()
        except Exception as rpc_exc:  # noqa: BLE001
            log.error(
                "regen: could not record the failure for %s (regen_failed: %s) — the row "
                "keeps its claim until the lease expires and its attempt is not counted.",
                rid, rpc_exc, exc_info=True,
            )


# ── the loop ─────────────────────────────────────────────────────────────

async def run_workers(
    workers: Sequence[Any],
    stop: asyncio.Event,
    poll_seconds: float = 10.0,
) -> None:
    """Call every worker's run_once() in a thread, sleep, repeat until stop is set."""
    while not stop.is_set():
        for w in workers:
            try:
                await asyncio.to_thread(w.run_once)
            except Exception as exc:  # noqa: BLE001
                log.error("worker %s poll failed: %s", type(w).__name__, exc, exc_info=True)
            if stop.is_set():
                return
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass
