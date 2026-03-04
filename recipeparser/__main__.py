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
from recipeparser.paths import get_default_output_dir

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
    args = parser.parse_args()

    if args.sync_categories:
        _cmd_sync_categories()
        return

    if not args.epub:
        parser.error("the following arguments are required: epub (path to .epub or .pdf)")

    from recipeparser.config import MAX_CONCURRENT_CAP
    concurrency = args.concurrency
    if concurrency is not None and (concurrency < 1 or concurrency > MAX_CONCURRENT_CAP):
        parser.error(
            f"--concurrency must be between 1 and {MAX_CONCURRENT_CAP} (got {concurrency})"
        )

    book_path = _resolve_book(args.epub)

    from recipeparser import process_epub
    from recipeparser.exceptions import RecipeParserError

    try:
        result = process_epub(
            book_path,
            args.output,
            units=args.units,
            concurrency=args.concurrency,
            rpm=args.rpm,
        )
        print(f"Export written to: {result}")
    except RecipeParserError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
