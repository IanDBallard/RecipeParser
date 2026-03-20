"""CLI entry point — python -m recipeparser <epub> [--output DIR] [--units ...]

Special flags (no epub required):
  --sync-categories   Pull the live category taxonomy from the local Paprika
                      database and overwrite recipeparser/categories.yaml.
"""
import argparse
import logging
import sys
from pathlib import Path

from recipeparser.paprika_db import find_paprika_db, read_categories_from_db
from recipeparser.categories import _CATEGORIES_FILE
from recipeparser.paths import get_default_output_dir, get_env_file
from recipeparser.adapters.cli import run_cli_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger(__name__)


def _resolve_book(raw: str) -> str:
    """
    Accept a path to an .epub or .pdf file, or a directory containing
    exactly one .epub or exactly one .pdf. Returns the resolved file path.
    """
    p = Path(raw)

    if p.is_file():
        if p.suffix.lower() not in (".epub", ".pdf"):
            print(
                f"Error: '{p}' is not an EPUB or PDF file.",
                file=sys.stderr,
            )
            sys.exit(1)
        return str(p)

    if p.is_dir():
        epubs = list(p.glob("*.epub"))
        pdfs = list(p.glob("*.pdf"))
        if len(epubs) == 1 and len(pdfs) == 0:
            log.info("Directory provided — using '%s'.", epubs[0].name)
            return str(epubs[0])
        if len(pdfs) == 1 and len(epubs) == 0:
            log.info("Directory provided — using '%s'.", pdfs[0].name)
            return str(pdfs[0])
        if len(epubs) > 1 or len(pdfs) > 1 or (len(epubs) == 1 and len(pdfs) == 1):
            print(
                "Error: Specify one file (directory has multiple .epub/.pdf).",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"Error: No .epub or .pdf file found in directory '{p}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Error: Path not found: '{p}'", file=sys.stderr)
    sys.exit(1)


# Backward compatibility for callers (e.g. tests) that use the old name
_resolve_epub = _resolve_book


def _units_to_uom(units: str) -> str:
    """Map the CLI --units flag value to the uom_system string expected by RecipePipeline.

    CLI flag values: "metric" | "us" | "imperial" | "book"
    Pipeline values: "Metric" | "US" | "Imperial"

    "book" means "preserve whatever the book uses" — we pass "US" as the
    default and let the pipeline's extraction prompt handle it naturally.
    """
    return {
        "metric": "Metric",
        "us": "US",
        "imperial": "Imperial",
        "book": "US",  # pipeline default; extraction prompt preserves book units
    }.get(units.lower(), "US")


def _cmd_sync_categories() -> None:
    """Pull the live Paprika category hierarchy and save it to categories.yaml."""
    import yaml

    db_path = find_paprika_db()
    if db_path is None:
        print(
            "Error: Could not locate a Paprika SQLite database on this machine.\n"
            "Make sure Paprika 3 has been installed and opened at least once.",
            file=sys.stderr,
        )
        sys.exit(1)

    log.info("Reading categories from: %s", db_path)
    data, order = read_categories_from_db(db_path)

    if not data and not order:
        print("Warning: No categories found in the Paprika database.", file=sys.stderr)
        sys.exit(1)

    categories_yaml = {"categories": {k: v for k, v in data.items()}}

    dest: Path = _CATEGORIES_FILE
    dest.write_text(yaml.dump(categories_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8")

    total_sub = sum(len(v) for v in data.values())
    print(
        f"Synced {len(data)} top-level categories "
        f"({total_sub} subcategories) → {dest}"
    )


def _cmd_merge(paths: list, output_dir: str) -> None:
    """Merge multiple .paprikarecipes archives into one."""
    from pathlib import Path as _Path
    from recipeparser.export import merge_exports
    from recipeparser.exceptions import RecipeParserError

    resolved = [_Path(p) for p in paths]
    missing = [str(p) for p in resolved if not p.exists()]
    if missing:
        print(f"Error: file(s) not found: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    try:
        out = merge_exports(resolved, _Path(output_dir))
        print(f"Merged export written to: {out}")
    except RecipeParserError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_recategorize(paprika_path: str, output_dir: str) -> None:
    """Re-run categorisation on an existing .paprikarecipes archive."""
    import os
    from pathlib import Path as _Path
    from recipeparser.recategorize import recategorize
    from recipeparser.exceptions import RecipeParserError

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        # Try loading from .env
        env_file = get_env_file()
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("GOOGLE_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    os.environ["GOOGLE_API_KEY"] = api_key
                    break

    if not api_key:
        print(
            "Error: GOOGLE_API_KEY not set. Set the environment variable or save it via the GUI.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        out = recategorize(_Path(paprika_path), client, _Path(output_dir))
        print(f"Recategorized export written to: {out}")
    except RecipeParserError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        _version = _pkg_version("recipeparser")
    except PackageNotFoundError:
        _version = "unknown"

    parser = argparse.ArgumentParser(
        description="Extract recipes from an EPUB cookbook and export to Paprika 3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  recipeparser cookbook.epub\n"
            "  recipeparser cookbook.epub --output ~/exports --units metric\n"
            "  recipeparser --folder /path/to/cookbooks --output ~/exports\n"
            "  recipeparser --merge a.paprikarecipes b.paprikarecipes --output ~/exports\n"
            "  recipeparser --recategorize cookbook.paprikarecipes\n"
            "  recipeparser --sync-categories\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_version}",
    )
    parser.add_argument(
        "epub",
        nargs="?",
        help="Path to an .epub or .pdf cookbook, or a directory containing one.",
    )
    parser.add_argument(
        "--output",
        default=str(get_default_output_dir()),
        help="Directory to write the .paprikarecipes file.",
    )
    parser.add_argument(
        "--units",
        choices=["metric", "us", "imperial", "book"],
        default="book",
        help=(
            "Unit-of-measure preference for dual-measurement books "
            "(e.g. '2 cups/250g flour'). "
            "'metric' keeps gram/ml values; 'us' keeps cup/tbsp values; "
            "'imperial' keeps oz/lb values; 'book' preserves whatever the book uses. "
            "Default: book."
        ),
    )
    parser.add_argument(
        "--sync-categories",
        action="store_true",
        help=(
            "Pull the live category hierarchy from the local Paprika database "
            "and save to the user categories file. No EPUB argument is needed."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Max in-flight Gemini API calls (1–10, default 1). "
            "When --rpm is set, RPM is the constraining factor."
        ),
    )
    parser.add_argument(
        "--rpm",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Requests per minute limit. When set, no more than N requests start "
            "in any 60s window. Omit for no RPM cap."
        ),
    )
    # ── Phase 3a: folder processing ───────────────────────────────────────────
    parser.add_argument(
        "--folder",
        metavar="DIR",
        help=(
            "Process all .epub and .pdf files found in DIR sequentially. "
            "Each book is exported to --output as a separate .paprikarecipes file."
        ),
    )
    # ── Phase 3a: merge exports ───────────────────────────────────────────────
    parser.add_argument(
        "--merge",
        nargs="+",
        metavar="FILE",
        help=(
            "Merge two or more .paprikarecipes archives into a single "
            "merged_<timestamp>.paprikarecipes file in --output. "
            "Duplicates (by normalised name) are removed."
        ),
    )
    # ── Phase 3d: recategorize ────────────────────────────────────────────────
    parser.add_argument(
        "--recategorize",
        metavar="FILE",
        help=(
            "Re-run Gemini categorisation on every recipe in FILE "
            "(.paprikarecipes) and write <stem>_recategorized.paprikarecipes "
            "to --output."
        ),
    )
    args = parser.parse_args()

    # ── Exclusive-mode dispatch ───────────────────────────────────────────────
    if args.sync_categories:
        _cmd_sync_categories()
        return

    if args.merge:
        _cmd_merge(args.merge, args.output)
        return

    if args.recategorize:
        _cmd_recategorize(args.recategorize, args.output)
        return

    if args.folder:
        folder = Path(args.folder)
        if not folder.is_dir():
            print(f"Error: --folder path is not a directory: '{folder}'", file=sys.stderr)
            sys.exit(1)
        books = sorted(folder.glob("*.epub")) + sorted(folder.glob("*.pdf"))
        if not books:
            print(f"Error: No .epub or .pdf files found in '{folder}'.", file=sys.stderr)
            sys.exit(1)

        from recipeparser.exceptions import RecipeParserError
        import os

        api_key = os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            env_file = get_env_file()
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    if line.startswith("GOOGLE_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        os.environ["GOOGLE_API_KEY"] = api_key
                        break
        if not api_key:
            print("Error: GOOGLE_API_KEY not set.", file=sys.stderr)
            sys.exit(1)

        from google import genai
        client = genai.Client(api_key=api_key)

        from recipeparser.config import MAX_CONCURRENT_CAP
        concurrency = args.concurrency
        if concurrency is not None and (concurrency < 1 or concurrency > MAX_CONCURRENT_CAP):
            parser.error(
                f"--concurrency must be between 1 and {MAX_CONCURRENT_CAP} (got {concurrency})"
            )

        errors = []
        for book in books:
            log.info("Processing: %s", book.name)
            try:
                result = run_cli_pipeline(
                    str(book),
                    args.output,
                    client,
                    uom_system=_units_to_uom(args.units),
                    concurrency=args.concurrency,
                    rpm=args.rpm,
                )
                print(f"  ✓ {book.name} → {result}")
            except (RecipeParserError, RuntimeError, ValueError) as e:
                log.error("  ✗ %s: %s", book.name, e)
                errors.append((book.name, str(e)))

        if errors:
            print(f"\n{len(errors)} book(s) failed:", file=sys.stderr)
            for name, msg in errors:
                print(f"  {name}: {msg}", file=sys.stderr)
            sys.exit(1)
        return

    # ── Single-file mode (original behaviour) ─────────────────────────────────
    if not args.epub:
        parser.error(
            "the following arguments are required: epub (path to .epub or .pdf), "
            "or use --folder / --merge / --recategorize / --sync-categories"
        )

    from recipeparser.config import MAX_CONCURRENT_CAP
    concurrency = args.concurrency
    if concurrency is not None and (concurrency < 1 or concurrency > MAX_CONCURRENT_CAP):
        parser.error(
            f"--concurrency must be between 1 and {MAX_CONCURRENT_CAP} (got {concurrency})"
        )

    book_path = _resolve_book(args.epub)

    import os
    from recipeparser.exceptions import RecipeParserError

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        env_file = get_env_file()
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("GOOGLE_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    os.environ["GOOGLE_API_KEY"] = api_key
                    break
    if not api_key:
        print(
            "Error: GOOGLE_API_KEY not set. Set the environment variable or save it via the GUI.",
            file=sys.stderr,
        )
        sys.exit(1)

    from google import genai
    client = genai.Client(api_key=api_key)

    try:
        result = run_cli_pipeline(
            book_path,
            args.output,
            client,
            uom_system=_units_to_uom(args.units),
            concurrency=args.concurrency,
            rpm=args.rpm,
        )
        print(f"Export written to: {result}")
    except (RecipeParserError, RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
