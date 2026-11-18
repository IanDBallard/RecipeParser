"""
recipeparser/adapters/cli.py — CLI adapter for the RecipePipeline.

Thin wrapper that wires the canonical pipeline stack for local file processing:

    Reader (EPUB/PDF) → RecipePipeline → PaprikaWriter

This is the Phase 7B replacement for the monolith ``process_epub()`` function
in ``recipeparser/pipeline.py``.  The CLI (``__main__.py``) imports
``run_cli_pipeline`` from here instead of the monolith.

Design constraints (§12 — Zero Technical Debt):
  - No Supabase dependency — uses YamlCategorySource for local taxonomy.
  - No async — the CLI is synchronous; RecipePipeline.run() is blocking.
  - Progress is printed to stdout via a simple on_progress callback.
  - Output is a .paprikarecipes ZIP written by PaprikaWriter.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, TypeVar

from recipeparser.config import MAX_CONCURRENT_API_CALLS
from recipeparser.core import quota_wait
from recipeparser.core.fsm import PipelineController
from recipeparser.core.pipeline import RecipePipeline
from recipeparser.io.category_sources.yaml_source import YamlCategorySource
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.pdf import PdfReader
from recipeparser.io.writers.paprika_zip import PaprikaWriter
from recipeparser.models import IngestResponse
from recipeparser.paths import get_categories_file, get_default_output_dir

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Exit statuses after a stop by signal, as a shell reports them (128 + the signal's number).
_EXIT_STATUS = {signal.SIGINT: 130, getattr(signal, "SIGTERM", 15): 143}


class _Stopped(BaseException):
    """Ctrl-C or SIGTERM, raised in the main thread by our handler (#161)."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _run_stoppably(work: Callable[[], T], on_stop: Callable[[int], None]) -> T:
    """Run ``work``, and on Ctrl-C or SIGTERM call ``on_stop`` and exit at once.

    The work runs on a daemon thread while the main thread waits in short joins, so a
    signal is heard within a fraction of a second even while the pipeline sleeps out a
    used-up daily quota for hours (#161). ``docker stop`` sends SIGTERM and kills the
    process ten seconds later, so the stop does no more than ``on_stop`` (save what was
    made) and ``os._exit``: a normal exit would wait on the pipeline's sleeping workers.

    Off the main thread (the GUI runs the CLI pipeline in a worker) no handler can be
    installed, so ``work`` runs as it is.
    """
    if threading.current_thread() is not threading.main_thread():
        return work()

    outcome: dict = {}

    def target() -> None:
        try:
            outcome["result"] = work()
        except BaseException as exc:  # noqa: BLE001 - handed back to the main thread
            outcome["error"] = exc

    def handler(signum: int, _frame: Any) -> None:
        raise _Stopped(signum)

    signals = [s for s in (signal.SIGINT, getattr(signal, "SIGTERM", None)) if s is not None]
    previous = {s: signal.getsignal(s) for s in signals}
    for s in signals:
        signal.signal(s, handler)
    try:
        worker = threading.Thread(target=target, name="recipeparser-pipeline", daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.25)
    except (_Stopped, KeyboardInterrupt) as stop:
        signum = stop.signum if isinstance(stop, _Stopped) else signal.SIGINT
        try:
            on_stop(signum)
        finally:
            os._exit(_EXIT_STATUS.get(signum, 1))
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def run_cli_pipeline(
    book_path: str,
    output_dir: Optional[str] = None,
    client: Any = None,
    *,
    concurrency: Optional[int] = None,
    rpm: Optional[int] = None,
    verbose: bool = True,
    controller: Optional[PipelineController] = None,
) -> str:
    """
    Process a single EPUB or PDF cookbook and write a .paprikarecipes archive.

    This is the canonical CLI entry point for Phase 7B+.  It replaces the
    monolith ``recipeparser.pipeline.process_epub()`` function.

    Args:
        book_path:          Absolute path to an .epub or .pdf file.
        output_dir:         Directory to write the output archive.
                            Defaults to ``get_default_output_dir()``.
        client:             An initialised ``google.genai.Client`` instance.
                            Required — raises ``ValueError`` if None.
        concurrency:        Max parallel Gemini API calls (default:
                            ``config.MAX_CONCURRENT_API_CALLS``, 1).
        rpm:                Optional RPM cap for the GlobalRateLimiter.
        verbose:            If True, print per-chunk progress to stdout.
        controller:         Optional PipelineController for Pause/Cancel (GUI use).

    Returns:
        The absolute path to the written .paprikarecipes archive.

    Raises:
        ValueError:  If ``client`` is None.
        RuntimeError: If the reader produces no chunks or the pipeline
                      produces no results (all chunks failed).
    """
    if client is None:
        raise ValueError(
            "run_cli_pipeline: 'client' must be an initialised google.genai.Client. "
            "Set GOOGLE_API_KEY and pass the client explicitly."
        )

    book = Path(book_path)
    out_dir = Path(output_dir) if output_dir else get_default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Read source file into Chunks ───────────────────────────────────────
    suffix = book.suffix.lower()
    if suffix == ".epub":
        reader = EpubReader()
    elif suffix == ".pdf":
        reader = PdfReader()
    else:
        raise ValueError(
            f"run_cli_pipeline: unsupported file type '{suffix}'. "
            "Only .epub and .pdf are supported."
        )

    log.info("Reading %s …", book.name)
    chunks = reader.read(str(book))

    if not chunks:
        raise RuntimeError(
            f"run_cli_pipeline: reader produced no chunks from '{book.name}'. "
            "The file may be empty or unreadable."
        )

    log.info("Produced %d chunk(s) from %s.", len(chunks), book.name)

    # ── 2. Wire category source (YAML — local, no Supabase) ───────────────────
    yaml_path = get_categories_file()
    category_source = YamlCategorySource(yaml_path=yaml_path if yaml_path.exists() else None)

    # ── 3. Build pipeline ─────────────────────────────────────────────────────
    if controller is None:
        controller = PipelineController()

    pipeline_kwargs: dict = dict(
        client=client,
        controller=controller,
        category_source=category_source,
    )
    # The CLI's own default, not RecipePipeline's: the pipeline defaults to 4 for
    # the server, while --help and the README promise 1, which a free-tier key
    # (5 requests a minute) can sustain (issue #149).
    pipeline_kwargs["concurrency"] = (
        concurrency if concurrency is not None else MAX_CONCURRENT_API_CALLS
    )
    if rpm is not None:
        pipeline_kwargs["rpm"] = rpm

    pipeline = RecipePipeline(**pipeline_kwargs)

    # ── 4. Progress callback ──────────────────────────────────────────────────
    def _on_progress(stage: str, completed: int, total: int) -> None:
        if verbose:
            print(f"  [{completed}/{total}] {stage}", flush=True)

    # ── 5. Run pipeline ───────────────────────────────────────────────────────
    # Every finished recipe is kept as it arrives, so a run that stops early (the daily
    # quota after 24 hours of waiting, an error, Ctrl-C, `docker stop`) saves what it made
    # to <book>-partial.paprikarecipes rather than losing it all (#161).
    made: List[IngestResponse] = []
    made_lock = threading.Lock()
    partial_path = out_dir / f"{book.stem}-partial.paprikarecipes"

    def _on_result(result: IngestResponse, _ai_processed: bool) -> None:
        with made_lock:
            made.append(result)

    def _save_partial(why: str) -> None:
        with made_lock:
            recipes = list(made)
        if not recipes:
            print(f"{why} No recipes were finished, so nothing was saved.", flush=True)
            return
        try:
            PaprikaWriter(output_path=partial_path).write(recipes)
        except Exception:  # noqa: BLE001 - the stop must still be reported
            log.exception("Could not save the partial archive %s.", partial_path)
            return
        print(f"{why} Saved {len(recipes)} recipe(s) to {partial_path}", flush=True)

    def _on_quota_pause(delay: float) -> None:
        with made_lock:
            count = len(made)
        print(f"  Waiting for Gemini's daily quota to reset. Ctrl-C stops and saves the {count} recipe(s) so far.",
              flush=True)

    def _on_stop(signum: int) -> None:
        name = "Ctrl-C" if signum == signal.SIGINT else "Stop signal"
        _save_partial(f"{name}: stopped.")

    log.info("Running pipeline on %d chunk(s) …", len(chunks))
    # The CLI waits out a used-up daily quota (and gives up after 24 hours); the server never does.
    quota_wait.install(quota_wait.QuotaWait(on_pause=_on_quota_pause))
    try:
        results = _run_stoppably(
            lambda: pipeline.run(chunks, on_progress=_on_progress, on_result=_on_result),
            _on_stop,
        )
    except Exception as exc:
        _save_partial(f"Stopped: {exc}")
        raise
    finally:
        quota_wait.install(None)

    if not results:
        raise RuntimeError(
            f"run_cli_pipeline: pipeline produced no results from '{book.name}'. "
            "All chunks may have failed — check logs for details."
        )

    log.info("Pipeline produced %d recipe(s).", len(results))

    # ── 6. Write output ───────────────────────────────────────────────────────
    stem = book.stem
    out_path = out_dir / f"{stem}.paprikarecipes"
    writer = PaprikaWriter(output_path=out_path)
    writer.write(results)

    log.info("Export written to: %s", out_path)
    return str(out_path)
