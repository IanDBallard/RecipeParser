"""CLI entry point — python -m recipeparser <epub> [--output DIR] [--units ...]"""
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def _resolve_epub(raw: str) -> str:
    """
    Accept either a direct path to an .epub file or a Calibre book folder
    (which contains exactly one .epub file).  Returns the resolved .epub path
    as a string, or raises SystemExit with a clear message if nothing is found.
    """
    p = Path(raw)

    if p.is_file():
        if p.suffix.lower() != ".epub":
            print(
                f"Error: '{p}' is not an .epub file.",
                file=sys.stderr,
            )
            sys.exit(1)
        return str(p)

    if p.is_dir():
        epubs = list(p.glob("*.epub"))
        if len(epubs) == 1:
            logging.getLogger(__name__).info(
                "Directory provided — using '%s'.", epubs[0].name
            )
            return str(epubs[0])
        if len(epubs) == 0:
            print(
                f"Error: No .epub file found in directory '{p}'.",
                file=sys.stderr,
            )
        else:
            names = ", ".join(e.name for e in epubs)
            print(
                f"Error: Multiple .epub files found in '{p}' — please specify one:\n  {names}",
                file=sys.stderr,
            )
        sys.exit(1)

    print(f"Error: Path not found: '{p}'", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Extract recipes from an EPUB cookbook and export to Paprika 3."
    )
    parser.add_argument(
        "epub",
        help="Path to the .epub file, or to a Calibre book folder containing one.",
    )
    parser.add_argument(
        "--output",
        default="./output",
        help="Directory to write the .paprikarecipes file (default: ./output).",
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
    args = parser.parse_args()

    epub_path = _resolve_epub(args.epub)

    from recipeparser import process_epub
    from recipeparser.exceptions import RecipeParserError

    try:
        result = process_epub(epub_path, args.output, units=args.units)
        print(f"Export written to: {result}")
    except RecipeParserError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
