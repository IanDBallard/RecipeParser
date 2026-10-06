"""Per-job bookkeeping shared by both ingestion endpoints.

Both /jobs and /jobs/file need the same three callbacks and the same terminal
payload, and api.py is long enough already. Keeping it here also means the
counting rules can be tested without standing up FastAPI or Supabase.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Set

from recipeparser.core.clock import utc_timestamp
from recipeparser.core.models import Chunk
from recipeparser.core.stages.tag import TagFailure
from recipeparser.io.writers.supabase import write_recipe_categories, write_recipe_to_supabase
from recipeparser.models import IngestResponse

log = logging.getLogger(__name__)

# The list rides a row that re-syncs on every stage change; the count stays
# truthful, so a pathological job cannot inflate what every client downloads.
SKIPPED_LIST_CAP = 50

# Keyword, not positional: write_recipe_to_supabase's third parameter is
# recipe_id, and category_ids is fourth. Passing three positionally would file
# every recipe's category map as its row id.
WriteFn = Callable[..., Any]


class JobSink:
    """Collects what one ingestion job did, and writes each recipe as it arrives."""

    def __init__(
        self,
        job_id: str,
        user_id: str,
        category_ids: Dict[str, str],
        write: WriteFn = write_recipe_to_supabase,
        now: Callable[[], str] = utc_timestamp,
        write_links: WriteFn = write_recipe_categories,
    ) -> None:
        self._job_id = job_id
        self._user_id = user_id
        self._category_ids = category_ids
        self._write = write
        self._write_links = write_links
        self._now = now
        # The row id each written recipe got, keyed by the object, so TAG's links
        # (which arrive after the recipe, Fix Roadmap F-246) reach the right row.
        self._row_ids: Dict[int, str] = {}
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
        # Which distinct sources the written recipes carry, so the job row can
        # point the library at it (design 2026-09-11: source_hint becomes the
        # source_key) — but only when the batch carries exactly one (ruling 7
        # amended): a Paprika restore's chunks each carry their own recipe's
        # citation, so "most common" would file an 800-recipe archive under
        # whichever site happened to have the most recipes in it.
        self._source_keys: Set[str] = set()
        # Category links the writer could not make for a recipe it DID write
        # (Fix Roadmap F-115, 2026-09-29): {"label", "category_id", "reason"}.
        # Deliberately not in `skipped`: the client reads every entry there as a
        # section that produced no recipe ("1 section produced no recipe", "1
        # missed"), which a recipe that lost one tag is not. No column the
        # client reads fits, so for now they reach the job's log and the
        # completion line; the count stays true while the list is capped.
        self.refused_link_count = 0
        self.refused_links: List[Dict[str, Any]] = []

    # ── callbacks handed to RecipePipeline.run ────────────────────────────────

    def on_result(self, recipe: IngestResponse) -> None:
        """Persist one finished recipe. A failure here costs that recipe, not the job."""
        title = getattr(recipe, "title", None)

        def _link_refused(link: Dict[str, str]) -> None:
            self.refused_link_count += 1
            if len(self.refused_links) < SKIPPED_LIST_CAP:
                self.refused_links.append({"label": title, **link})
            log.warning(
                "Job %s: recipe %r was written, but its link to category %s was refused: %s",
                self._job_id, title, link.get("category_id"), link.get("reason"),
            )

        try:
            rid = self._write(
                recipe, self._user_id,
                category_ids=self._category_ids, on_link_refused=_link_refused,
            )
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
        if isinstance(rid, str):
            self._row_ids[id(recipe)] = rid
        key = getattr(recipe, "source_key", None)
        if key:
            self._source_keys.add(key)

    def on_tags(self, recipe: IngestResponse) -> None:
        """Write the links TAG gave a recipe already written (F-246). Never raises."""
        rid = self._row_ids.get(id(recipe))
        if rid is None or not recipe.grid_categories:
            return
        title = recipe.title

        def _link_refused(link: Dict[str, str]) -> None:
            self.refused_link_count += 1
            if len(self.refused_links) < SKIPPED_LIST_CAP:
                self.refused_links.append({"label": title, **link})
            log.warning(
                "Job %s: recipe %r was tagged, but its link to category %s was refused: %s",
                self._job_id, title, link.get("category_id"), link.get("reason"),
            )

        try:
            self._write_links(
                rid, self._user_id, recipe.grid_categories, self._category_ids,
                on_link_refused=_link_refused,
            )
        except Exception:
            log.exception("Job %s: the tags for recipe %r could not be written.", self._job_id, title)

    def on_tag_failed(self, failure: TagFailure) -> None:
        """An axis TAG could not ask for a batch (F-246, design D8): counted with the refused links."""
        for title in failure.titles:
            self.refused_link_count += 1
            if len(self.refused_links) < SKIPPED_LIST_CAP:
                self.refused_links.append({"label": title, "axis": failure.axis, "reason": failure.reason})
        log.warning(
            "Job %s: the %r axis could not be tagged for %d recipe(s): %s",
            self._job_id, failure.axis, len(failure.titles), failure.reason,
        )

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
        if len(self._source_keys) == 1:
            # Pasted text and photos only learn their source from the model, so
            # the read-time hint (the chunks' citation) was never set for them;
            # a job that wrote nothing keyed, or whose written recipes carry
            # two or more distinct keys (a mixed Paprika restore), keeps
            # whatever hint it had rather than filing it under one of many.
            (only_key,) = self._source_keys
            payload["source_hint"] = only_key
        if error_message:
            payload["error_message"] = error_message
        return payload

    # ── internals ─────────────────────────────────────────────────────────────

    def _record_skip(self, label: Optional[str], reason: str, index: int) -> None:
        self.skipped_count += 1
        if len(self.skipped) < SKIPPED_LIST_CAP:
            self.skipped.append({"label": label, "index": index, "reason": reason})
