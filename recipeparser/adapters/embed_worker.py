"""
recipeparser/adapters/embed_worker.py — keeps every recipe's embedding current
(Cayenne AI search §6).

The queue is the recipes table: claim_stale_embeddings hands back rows whose
embedded_rev lags embed_rev, with the title, ingredient lines, direction steps
and category names the embedding text is built from. The vector is written
guarded by embed_rev, so an edit that lands mid-run is never overwritten by a
stale vector. Writing these columns never bumps body_rev. A failure is recorded
through embed_failed; the database stops offering a row after three failures at
one embed_rev. run_workers drives run_once exactly as it drives RegenWorker.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List

from recipeparser import usage
from recipeparser.core.stages.embed import _EXPECTED_DIMS, build_embedding_text
from recipeparser.gemini import get_embeddings

log = logging.getLogger(__name__)


class EmbedWorker:
    def __init__(
        self,
        supabase: Any,
        gemini_client: Any,
        *,
        embed_text_fn: Callable[..., List[float]] = get_embeddings,
        batch: int = 10,
        concurrency: int = 2,
        ai_allowed: Callable[[str], bool] = lambda _u: True,
        usage_client: Callable[[], Any] = lambda: None,
    ) -> None:
        self._sb = supabase
        self._client = gemini_client
        self._embed_text = embed_text_fn
        self._batch = max(1, batch)
        self._concurrency = max(1, concurrency)
        self._ai_allowed = ai_allowed
        self._usage_client = usage_client

    def run_once(self) -> int:
        rows = self._sb.rpc("claim_stale_embeddings", {"p_limit": self._batch}).execute().data or []
        if not rows:
            return 0
        log.info("embed: claimed %d recipe(s).", len(rows))
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            list(pool.map(self._process, rows))
        return len(rows)

    def _process(self, row: Dict[str, Any]) -> None:
        # The unpack is inside the try so a malformed row cannot abandon the batch.
        rid = row.get("id", "?")
        owner = str(row.get("user_id") or "")
        if not self._ai_allowed(owner):
            # Paused (AI caps D5): leave the claim to lapse so the queue moves on to
            # other owners' rows; it is re-checked when the lease expires.
            return
        with usage.attributed(owner, "embed", get_client=self._usage_client):
            try:
                rid, rev = row["id"], int(row["embed_rev"])
                text = build_embedding_text(
                    row["title"],
                    row.get("category_names") or [],
                    row.get("ingredient_lines") or [],
                    row.get("direction_steps") or [],
                )
                vector = self._embed_text(text, self._client)
                if len(vector) != _EXPECTED_DIMS:
                    raise ValueError(f"expected {_EXPECTED_DIMS}-dim vector, got {len(vector)}")
                res = (
                    self._sb.table("recipes")
                    .update({"embedding": vector, "embedded_rev": rev, "embed_claimed_at": None})
                    .eq("id", rid)
                    .eq("embed_rev", rev)
                    .execute()
                )
                if not res.data:
                    log.info("embed: %s was edited again during the run (rev %d) — result dropped.", rid, rev)
                    self._release_claim(rid)
                else:
                    log.info("embed: %s embedded at rev %d.", rid, rev)
            except Exception as exc:  # noqa: BLE001 — every failure is recorded, never fatal
                log.warning("embed: %s failed: %s", rid, exc, exc_info=True)
                self._record_failure(rid)

    def _release_claim(self, rid: str) -> None:
        """Clear the claim on a row whose result was dropped, so its new revision is
        picked up at the next poll instead of after the lease. Never raises; a failed
        release is not a failure of the row (its new revision has not been tried)."""
        try:
            self._sb.table("recipes").update({"embed_claimed_at": None}).eq("id", rid).execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("embed: could not release the claim on %s (%s) — it waits out its lease.",
                        rid, exc, exc_info=True)

    def _record_failure(self, rid: str) -> None:
        try:
            self._sb.rpc("embed_failed", {"p_id": rid}).execute()
        except Exception as rpc_exc:  # noqa: BLE001
            log.error("embed: could not record the failure for %s (embed_failed: %s) — the row "
                      "keeps its claim until the lease expires.", rid, rpc_exc, exc_info=True)
