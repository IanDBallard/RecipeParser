"""Per-job bookkeeping shared by both ingestion endpoints.

Both /jobs and /jobs/file need the same three callbacks and the same terminal
payload, and api.py is long enough already. Keeping it here also means the
counting rules can be tested without standing up FastAPI or Supabase.
"""
from __future__ import annotations

import datetime
import logging
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from recipeparser.core.models import Chunk
from recipeparser.io.writers.supabase import write_recipe_to_supabase
from recipeparser.models import IngestResponse

log = logging.getLogger(__name__)

# The list rides a row that re-syncs on every stage change; the count stays
# truthful, so a pathological job cannot inflate what every client downloads.
SKIPPED_LIST_CAP = 50

# Keyword, not positional: write_recipe_to_supabase's third parameter is
# recipe_id, and category_ids is fourth. Passing three positionally would file
# every recipe's category map as its row id.
WriteFn = Callable[..., Any]


def _utc_now() -> str:
    # datetime.utcnow() is deprecated (and scheduled for removal); the
    # timezone-aware replacement's isoformat() already ends in "+00:00", so
    # appending "Z" to that would yield the malformed "+00:00Z" — strftime
    # sidesteps it and still lands a valid ISO-8601 UTC instant in the
    # ingestion_jobs.updated_at timestamptz column.
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


class JobSink:
    """Collects what one ingestion job did, and writes each recipe as it arrives."""

    def __init__(
        self,
        job_id: str,
        user_id: str,
        category_ids: Dict[str, str],
        write: WriteFn = write_recipe_to_supabase,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        self._job_id = job_id
        self._user_id = user_id
        self._category_ids = category_ids
        self._write = write
        self._now = now
        self.recipe_count = 0
        self.skipped_count = 0
        self.skipped: List[Dict[str, Any]] = []
        self.progress_updates: List[int] = []
        # A job starts at 0% implicitly (the caller already knows that), so the
        # sentinel is 0, not -1 — otherwise the very first callback (which is
        # often itself 0%, e.g. completed=1 of a large total) would count as a
        # "change" and both 0 and 100 would always be emitted: 101 updates for
        # a job that is supposed to cap at 100.
        self._last_pct = 0
        # Which source the written recipes carry, so the job row can point the
        # library at it (design 2026-09-11: source_hint becomes the source_key).
        self._source_keys: Counter = Counter()

    # ── callbacks handed to RecipePipeline.run ────────────────────────────────

    def on_result(self, recipe: IngestResponse) -> None:
        """Persist one finished recipe. A failure here costs that recipe, not the job."""
        try:
            self._write(recipe, self._user_id, category_ids=self._category_ids)
        except Exception as exc:
            log.exception(
                "Job %s: failed to write recipe %r — counting it as skipped.",
                self._job_id,
                getattr(recipe, "title", "?"),
            )
            # No chunk survives to this point, so there is no submission position to
            # report — -1 tells a consumer this was a write failure, not a lost chunk.
            self._record_skip(getattr(recipe, "title", None), f"write failed: {exc}", -1)
            return
        self.recipe_count += 1
        key = getattr(recipe, "source_key", None)
        if key:
            self._source_keys[key] += 1

    def on_skip(self, chunk: Chunk, reason: str, index: int) -> None:
        """Record a chunk that produced nothing because something failed.

        ``index`` is the chunk's zero-based submission position in the batch
        (see RecipePipeline.run's on_skip contract) — for a PDF or EPUB import
        it is the only identification a lost chunk has, since those readers
        never set ``chunk.label``.
        """
        self._record_skip(chunk.label, reason, index)

    def on_progress(self, stage: str, completed: int, total: int) -> None:
        """Note a whole-percent change. Sub-percent ticks are dropped, not written."""
        if total <= 0:
            return
        pct = round(100 * completed / total)
        if pct == self._last_pct:
            return
        self._last_pct = pct
        self.progress_updates.append(pct)

    # ── terminal state ────────────────────────────────────────────────────────

    def finalize_payload(
        self,
        success: bool,
        error_message: Optional[str] = None,
        cancelled: bool = False,
    ) -> Dict[str, Any]:
        """The ingestion_jobs UPDATE for a finished job.

        progress_pct is present only on an uncancelled success. Writing 0 on
        failure told the client a job that died at 60% had never started, and a
        cancelled job is the same case: it keeps whatever the last update wrote.

        ``stage`` stays "DONE" for a cancelled run. stage says how far the
        pipeline got; status says how it ended. A "CANCELLED" stage value would
        break the client's IngestionStage union and its label map (spec 5.1).

        A run that raised is an error whatever was requested of it: ``success``
        is checked first so a cancel racing an exception cannot relabel the
        failure as a tidy stop.
        """
        if not success:
            status = "error"
        elif cancelled:
            status = "cancelled"
        else:
            status = "done"
        payload: Dict[str, Any] = {
            "status": status,
            "stage": "DONE" if success else "ERROR",
            "recipe_count": self.recipe_count,
            "skipped_count": self.skipped_count,
            "skipped": self.skipped,
            "updated_at": self._now(),
        }
        if success and not cancelled:
            payload["progress_pct"] = 100
        if self._source_keys:
            # Pasted text and photos only learn their source from the model, so
            # the read-time hint (the chunks' citation) was never set for them;
            # a job that wrote nothing keyed keeps whatever hint it had.
            payload["source_hint"] = self._source_keys.most_common(1)[0][0]
        if error_message:
            payload["error_message"] = error_message
        return payload

    # ── internals ─────────────────────────────────────────────────────────────

    def _record_skip(self, label: Optional[str], reason: str, index: int) -> None:
        self.skipped_count += 1
        if len(self.skipped) < SKIPPED_LIST_CAP:
            self.skipped.append({"label": label, "index": index, "reason": reason})
