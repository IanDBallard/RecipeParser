"""CLI entry point — python -m recipeparser <epub> [--output DIR]"""
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(
        description="Extract recipes from an EPUB cookbook and export to Paprika 3."
    )
    parser.add_argument("epub", help="Path to the input .epub file.")
    parser.add_argument(
        "--output",
        default="./output",
        help="Directory to write the .paprikarecipes file (default: ./output).",
    )
    args = parser.parse_args()

    from recipeparser import process_epub

    result = process_epub(args.epub, args.output)
    if result:
        print(f"Export written to: {result}")
    else:
        print("No recipes exported — check the log for details.")


if __name__ == "__main__":
    main()
