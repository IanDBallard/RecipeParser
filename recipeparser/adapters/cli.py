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
from pathlib import Path
from typing import Any, Optional

from recipeparser.core.fsm import PipelineController
from recipeparser.core.pipeline import RecipePipeline
from recipeparser.io.category_sources.yaml_source import YamlCategorySource
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.pdf import PdfReader
from recipeparser.io.writers.paprika_zip import PaprikaWriter
from recipeparser.paths import get_categories_file, get_default_output_dir

log = logging.getLogger(__name__)


def run_cli_pipeline(
    book_path: str,
    output_dir: Optional[str] = None,
    client: Any = None,
    *,
    uom_system: str = "US",
    measure_preference: str = "Volume",
    concurrency: Optional[int] = None,
    rpm: Optional[int] = None,
    verbose: bool = True,
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
        uom_system:         "US" | "Metric" | "Imperial" (default "US").
        measure_preference: "Volume" | "Weight" (default "Volume").
        concurrency:        Max parallel Gemini API calls (default: pipeline default).
        rpm:                Optional RPM cap for the GlobalRateLimiter.
        verbose:            If True, print per-chunk progress to stdout.

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
    controller = PipelineController()

    pipeline_kwargs: dict = dict(
        client=client,
        controller=controller,
        category_source=category_source,
        uom_system=uom_system,
        measure_preference=measure_preference,
    )
    if concurrency is not None:
        pipeline_kwargs["concurrency"] = concurrency
    if rpm is not None:
        pipeline_kwargs["rpm"] = rpm

    pipeline = RecipePipeline(**pipeline_kwargs)

    # ── 4. Progress callback ──────────────────────────────────────────────────
    def _on_progress(stage: str, completed: int, total: int) -> None:
        if verbose:
            print(f"  [{completed}/{total}] {stage}", flush=True)

    # ── 5. Run pipeline ───────────────────────────────────────────────────────
    log.info("Running pipeline on %d chunk(s) …", len(chunks))
    results = pipeline.run(chunks, on_progress=_on_progress)

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
