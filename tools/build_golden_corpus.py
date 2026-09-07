"""Build the golden corpus under tests/goldens/corpus/.

The built files are committed; this script exists so a reviewer can see exactly
how each fixture was made and rebuild it if a dependency changes.

Usage:
    python tools/build_golden_corpus.py --all          # needs network once, for Gutenberg
    python tools/build_golden_corpus.py --synthetic    # offline: everything but Gutenberg
"""
from __future__ import annotations

import argparse
import base64
import gzip
import io
import json
import os
import sys
import zipfile
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "goldens" / "corpus"

# ---------------------------------------------------------------------------
# Hand-rebuilt sources.  Layout features only; no author, no book name; two
# recipes each.  See corpus/README.md for the provenance note these back.
# ---------------------------------------------------------------------------

DUAL_UNITS_CHAPTERS = [
    (
        "Buttermilk Scones",
        """<h1>Buttermilk Scones</h1>
<p><img src="images/scones.jpg" alt="scones"/></p>
<h2>Ingredients</h2>
<ul>
<li>2 cups/250g plain flour</li>
<li>1 tablespoon/12g baking powder</li>
<li>1/2 teaspoon/3g fine salt</li>
<li>5 tablespoons/70g cold butter, cubed</li>
<li>3/4 cup/180ml buttermilk</li>
</ul>
<h2>Method</h2>
<ol>
<li>Heat the oven to 425F. Line a tray with parchment.</li>
<li>Rub the butter into the flour, baking powder and salt until it looks like coarse crumbs.</li>
<li>Stir in the buttermilk until a shaggy dough forms. Do not overwork it.</li>
<li>Pat out to 1 inch thick, cut rounds, and bake for 12 to 14 minutes.</li>
</ol>
<p>Serves 8. Prep 15 mins. Cook 14 mins.</p>""",
    ),
    (
        "Brown Butter Shortbread",
        """<h1>Brown Butter Shortbread</h1>
<h2>Ingredients</h2>
<ul>
<li>14 tablespoons/200g butter</li>
<li>1/2 cup/100g caster sugar</li>
<li>2 cups/250g plain flour</li>
<li>1/4 teaspoon/1.5g fine salt</li>
</ul>
<p><img src="images/shortbread.jpg" alt="shortbread"/></p>
<h2>Method</h2>
<ol>
<li>Brown the butter in a pale pan until it smells of hazelnuts, then cool until thick.</li>
<li>Beat in the sugar, then the flour and salt, to a stiff paste.</li>
<li>Press into a tin, dock all over, and bake at 325F for 40 minutes.</li>
<li>Cut while warm and cool in the tin.</li>
</ol>
<p>Serves 12. Prep 20 mins. Cook 40 mins.</p>""",
    ),
]

PHASES_BAKERS_CHAPTERS = [
    (
        "Overnight Country Loaf",
        """<h1>Overnight Country Loaf</h1>
<h2>Ingredients</h2>
<p>PHASE 1 &mdash; Levain</p>
<ul>
<li>28g whole wheat flour</li>
<li>28g water</li>
<li>6g ripe starter</li>
</ul>
<p>PHASE 2 &mdash; Final Dough</p>
<ul>
<li>450g bread flour</li>
<li>50g whole wheat flour</li>
<li>360g water</li>
<li>10g fine salt</li>
</ul>
<h2>Method</h2>
<p>PHASE 1</p>
<ol>
<li>Mix the levain ingredients and leave at room temperature for 10 hours.</li>
</ol>
<p>PHASE 2</p>
<ol>
<li>Mix the flours and water and rest for 40 minutes.</li>
<li>Add the levain and salt, then fold every 30 minutes for 3 hours.</li>
<li>Shape, retard overnight, and bake at 475F in a covered pot for 20 minutes, then 20 uncovered.</li>
</ol>
<p>Serves 10. Prep 14 hours. Cook 40 mins.</p>""",
    ),
    (
        "Baker's Percentage Table Loaf",
        """<h1>Sandwich Loaf</h1>
<h2>Ingredients</h2>
<p>Ingredient</p>
<p>Weight</p>
<p>Volume</p>
<p>Baker's %</p>
<p>Bread flour</p>
<p>500g</p>
<p>4 cups</p>
<p>100%</p>
<p>Water</p>
<p>325g</p>
<p>1 1/3 cups</p>
<p>65%</p>
<p>Salt</p>
<p>10g</p>
<p>1 3/4 tsp</p>
<p>2%</p>
<p>Instant yeast</p>
<p>7g</p>
<p>2 1/4 tsp</p>
<p>1.4%</p>
<p>Butter</p>
<p>30g</p>
<p>2 tbsp</p>
<p>6%</p>
<h2>Method</h2>
<ol>
<li>Mix everything to a smooth dough and knead for 8 minutes.</li>
<li>Prove until doubled, shape into a pan loaf, and prove again.</li>
<li>Bake at 400F for 35 minutes.</li>
</ol>
<p>Serves 12. Prep 3 hours. Cook 35 mins.</p>""",
    ),
]

SAVED_PAGE_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Weeknight Tomato Soup</title></head>
<body>
<nav><a href="/">Home</a> <a href="/recipes">Recipes</a></nav>
<article>
<h1>Weeknight Tomato Soup</h1>
<p class="meta">Serves 4 &middot; Prep 10 mins &middot; Cook 25 mins</p>
<h2>Ingredients</h2>
<ul>
<li>2 tablespoons olive oil</li>
<li>1 onion, sliced thin</li>
<li>3 cloves garlic, crushed</li>
<li>1 (28 ounce) can whole peeled tomatoes</li>
<li>2 cups vegetable stock</li>
<li>Salt and black pepper to taste</li>
</ul>
<h2>Directions</h2>
<ol>
<li>Warm the oil and soften the onion for 8 minutes without colouring it.</li>
<li>Add the garlic and cook for 1 minute more.</li>
<li>Tip in the tomatoes and stock, crushing the tomatoes with a spoon.</li>
<li>Simmer for 15 minutes, blend smooth, and season.</li>
</ol>
</article>
<footer><p>&copy; 2026 Example Kitchen</p></footer>
</body>
</html>
"""

# A 1x1 red JPEG — small, real, and enough to exercise photo_data end to end.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def _write_epub(path: Path, title: str, chapters, images) -> None:
    """Write a minimal but real EPUB with a genuine nav + NCX toc."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier(f"golden-{path.stem}")
    book.set_title(title)
    book.set_language("en")

    # Front matter the is_recipe_candidate() filter must reject.
    front = epub.EpubHtml(title="Front Matter", file_name="front.xhtml", lang="en")
    front.content = (
        "<h1>About This Collection</h1><p>These pages were rebuilt by hand to "
        "exercise a parser. They contain no attribution and no book title.</p>"
    )
    book.add_item(front)

    items = [front]
    for index, (chapter_title, html) in enumerate(chapters, start=1):
        item = epub.EpubHtml(title=chapter_title, file_name=f"chap{index}.xhtml", lang="en")
        item.content = html
        book.add_item(item)
        items.append(item)

    for name, data in images.items():
        img = epub.EpubImage()
        img.file_name = f"images/{name}"
        img.media_type = "image/jpeg"
        img.content = data
        book.add_item(img)

    book.toc = tuple(items)
    book.spine = ["nav", *items]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)


def build_dual_units() -> None:
    _write_epub(
        CORPUS / "dual-units.epub",
        "Dual Units",
        DUAL_UNITS_CHAPTERS,
        {"scones.jpg": TINY_JPEG, "shortbread.jpg": TINY_JPEG},
    )


def build_phases_bakers() -> None:
    _write_epub(
        CORPUS / "phases-bakers.epub",
        "Phases And Percentages",
        PHASES_BAKERS_CHAPTERS,
        {},
    )


def build_saved_page() -> None:
    (CORPUS / "saved-page.html").write_text(SAVED_PAGE_HTML, encoding="utf-8")


def build_legacy_photo(source_text: str) -> None:
    """One legacy Paprika entry (no _cayenne_meta) carrying base64 photo_data."""
    entry = {
        "name": "Boiled Custard",
        "ingredients": source_text.split("@@INGREDIENTS@@")[1].split("@@DIRECTIONS@@")[0].strip(),
        "directions": source_text.split("@@DIRECTIONS@@")[1].strip(),
        "servings": "6",
        "prep_time": "10 mins",
        "cook_time": "20 mins",
        "notes": "",
        "categories": [],
        "source": "",
        "source_url": "",
        "photo": "custard.jpg",
        "photo_data": base64.b64encode(TINY_JPEG).decode("ascii"),
    }
    out = CORPUS / "legacy-photo.paprikarecipes"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "Boiled Custard.paprikarecipe",
            gzip.compress(json.dumps(entry, ensure_ascii=False).encode("utf-8")),
        )


def build_text_pages(pages: list[str]) -> None:
    """A four-page PDF with a real text layer and one embedded image."""
    import re

    import fitz

    doc = fitz.open()
    for index, body in enumerate(pages[:4]):
        page = doc.new_page(width=612, height=792)
        # NOTE: each entry in `pages` is a whole trimmed Gutenberg chapter
        # (tens of thousands of characters) — far more than a single Letter
        # page can hold at fontsize 10. Two problems compound:
        #  1. PyMuPDF's insert_textbox() does not paginate: once the text
        #     overflows the box by more than a small margin it inserts
        #     *nothing at all* rather than as much as fits.
        #  2. `html_to_text()` joins text with "\n" per tag boundary, so the
        #     source is far denser in newlines than natural prose — each
        #     newline forces a fresh line in the box regardless of how much
        #     of the line's width was used, burning through the box's
        #     vertical space long before a character-count truncation alone
        #     would suggest.
        # Collapse whitespace to single spaces (turning it back into flowing
        # prose) before truncating, so a healthy amount of real text
        # reliably fits and the page keeps a genuine text layer.
        flat = re.sub(r"\s+", " ", body).strip()
        page.insert_textbox(fitz.Rect(54, 54, 558, 738), flat[:2500], fontsize=10, fontname="helv")
        if index == 1:
            page.insert_image(fitz.Rect(400, 600, 500, 700), stream=TINY_JPEG)
    doc.set_metadata({"title": "Text Pages", "author": "Golden Corpus"})
    doc.save(str(CORPUS / "text-pages.pdf"), garbage=4, deflate=True)
    doc.close()


def build_scanned() -> None:
    """Two rendered page images with no text layer at all."""
    import fitz

    src = fitz.open(str(CORPUS / "text-pages.pdf"))
    out = fitz.open()
    for index in range(min(2, src.page_count)):
        pixmap = src[index].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        page = out.new_page(width=pixmap.width, height=pixmap.height)
        page.insert_image(fitz.Rect(0, 0, pixmap.width, pixmap.height), stream=pixmap.tobytes("png"))
    src.close()
    out.set_metadata({"title": "Scanned", "author": "Golden Corpus"})
    out.save(str(CORPUS / "scanned.pdf"), garbage=4, deflate=True)
    out.close()


def fetch_and_trim_gutenberg() -> dict:
    """Download a public-domain Gutenberg cookbook and trim it to six chapters.

    Returns the provenance dict the README entry is written from.  The trimmed
    book's nav and NCX are regenerated by ebooklib, so the fixture still carries
    a real two-file TOC rather than a hand-written stub.
    """
    import requests
    from ebooklib import epub, ITEM_DOCUMENT

    index = requests.get(
        "https://gutendex.com/books",
        # NOTE: gutendex's "topic" query param matches subjects/bookshelves
        # exactly/by substring; "topic": "cookery" returns 0 results against
        # the live API (verified 2026-09-07), so the whole fetch would fail
        # before ever reaching a candidate. "search" (full-text relevance
        # search) with the same literal string "cookery" returns 68 results
        # led by real cookbooks (e.g. #29728 "Cookery and Dining in Imperial
        # Rome"). This is the one deliberate deviation from the brief's
        # verbatim script: the query parameter name, not the search term.
        params={"search": "cookery", "languages": "en"},
        timeout=60,
    )
    index.raise_for_status()
    for book_meta in index.json()["results"]:
        url = next(
            (u for mime, u in book_meta["formats"].items()
             if mime.startswith("application/epub+zip")),
            None,
        )
        if not url:
            continue
        payload = requests.get(url, timeout=120)
        payload.raise_for_status()
        scratch = CORPUS / "_gutenberg_raw.epub"
        scratch.write_bytes(payload.content)
        source = epub.read_epub(str(scratch))
        docs = [i for i in source.get_items_of_type(ITEM_DOCUMENT) if i.get_name().endswith((".xhtml", ".html", ".htm"))]
        if len(docs) < 7:
            scratch.unlink()
            continue

        trimmed = epub.EpubBook()
        trimmed.set_identifier("golden-gutenberg-multi")
        trimmed.set_title("Gutenberg Multi (trimmed)")
        trimmed.set_language("en")
        kept = []
        for order, item in enumerate(docs[:7]):
            new = epub.EpubHtml(
                title=item.get_name(),
                file_name=f"part{order}.xhtml",
                lang="en",
            )
            # NOTE: keep bytes, do not decode to str. The source Gutenberg
            # documents carry a leading `<?xml ... encoding="utf-8"?>`
            # declaration; lxml's parse_html_string refuses a Python str
            # with an encoding declaration ("Unicode strings with encoding
            # declaration are not supported"), which ebooklib's own
            # get_body_content() swallows into an empty string, later
            # surfacing as `lxml.etree.ParserError: Document is empty`
            # from write_epub()'s nav generation. Bytes parse fine.
            new.content = item.get_content()
            trimmed.add_item(new)
            kept.append(new)
        trimmed.toc = tuple(kept)
        trimmed.spine = ["nav", *kept]
        trimmed.add_item(epub.EpubNcx())
        trimmed.add_item(epub.EpubNav())
        epub.write_epub(str(CORPUS / "gutenberg-multi.epub"), trimmed)

        plain = []
        for item in kept:
            from recipeparser.utils import html_to_text

            plain.append(html_to_text(item.content))
        (CORPUS / "_gutenberg_text.txt").write_text("\n\n@@PAGE@@\n\n".join(plain), encoding="utf-8")
        scratch.unlink()
        return {
            "id": book_meta["id"],
            "title": book_meta["title"],
            "authors": ", ".join(a["name"] for a in book_meta["authors"]),
            "url": url,
        }
    raise SystemExit("No suitable Gutenberg cookbook found — widen the search and retry.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Fetch Gutenberg, then build everything.")
    parser.add_argument("--synthetic", action="store_true", help="Build everything but the Gutenberg fixture.")
    args = parser.parse_args()

    CORPUS.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    if args.all:
        provenance = fetch_and_trim_gutenberg()
        print("Gutenberg provenance for corpus/README.md:", json.dumps(provenance, indent=2))

    pages_file = CORPUS / "_gutenberg_text.txt"
    if not pages_file.exists():
        raise SystemExit("Run with --all first: text-pages.pdf is built from the Gutenberg text.")
    pages = pages_file.read_text(encoding="utf-8").split("\n\n@@PAGE@@\n\n")

    build_dual_units()
    build_phases_bakers()
    build_saved_page()
    build_text_pages(pages)
    build_scanned()
    build_legacy_photo(
        "@@INGREDIENTS@@\n1 quart milk\n4 eggs\n1/2 cup sugar\n1 teaspoon vanilla\n"
        "@@DIRECTIONS@@\nScald the milk.\nBeat the eggs and sugar together.\n"
        "Pour the hot milk over the eggs, stirring.\nCook over water until it coats a spoon.\n"
        "Cool, then stir in the vanilla."
    )
    pages_file.unlink()

    total = sum(p.stat().st_size for p in CORPUS.iterdir())
    print(f"corpus total: {total} bytes")
    if total >= 2 * 1024 * 1024:
        raise SystemExit(f"corpus is {total} bytes — over the 2 MB budget; trim further")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
