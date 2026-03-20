"""
Shared fixtures for RecipeParser live E2E tests (CLI, GUI).

Provides minimal EPUB/PDF generators and helpers. Used by live_cli_test.py
and live_gui_e2e.py.
"""
from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path


def make_epub(title: str = "Test Pancakes") -> tuple[bytes, str]:
    """Generate a minimal valid EPUB. Returns (bytes, tmp_path). Caller must unlink."""
    from ebooklib import epub as E
    import tempfile

    book = E.EpubBook()
    book.set_identifier("test-fixture-001")
    book.set_title(title)
    book.set_language("en")
    book.add_author("Test Kitchen")
    html = (
        f"<html><body><h1>{title}</h1><p>Servings: 4</p>"
        "<h2>Ingredients</h2><ul>"
        "<li>2 cups all-purpose flour</li>"
        "<li>2 tbsp sugar</li><li>1 tsp baking powder</li>"
        "<li>1 cup milk</li><li>2 eggs</li>"
        "<li>2 tbsp butter, melted</li>"
        "</ul><h2>Directions</h2><ol>"
        "<li>Mix dry ingredients in a bowl.</li>"
        "<li>Whisk wet ingredients separately.</li>"
        "<li>Combine wet and dry; stir until just mixed.</li>"
        "<li>Cook on a greased griddle over medium heat, 2 min per side.</li>"
        "</ol></body></html>"
    )
    ch = E.EpubHtml(title=title, file_name="chapter1.xhtml", lang="en")
    ch.set_content(html)
    book.add_item(ch)
    book.add_item(E.EpubNcx())
    book.add_item(E.EpubNav())
    book.spine = ["nav", ch]
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp_path = tmp.name
    E.write_epub(tmp_path, book)
    return Path(tmp_path).read_bytes(), tmp_path


def make_pdf(title: str = "Test Beef Stew") -> tuple[bytes, str]:
    """Generate a minimal PDF. Returns (bytes, tmp_path). Caller must unlink."""
    import fitz
    import tempfile

    doc = fitz.open()
    page = doc.new_page()
    text = "\n".join([
        title,
        "Servings: 6",
        "",
        "Ingredients:",
        "2 lbs beef chuck",
        "3 carrots, sliced",
        "3 potatoes, cubed",
        "1 onion, diced",
        "2 cups beef broth",
        "1 tbsp tomato paste",
        "",
        "Directions:",
        "1. Brown beef in batches.",
        "2. Add vegetables and broth.",
        "3. Simmer 90 minutes until tender.",
    ])
    page.insert_text((72, 72), text, fontsize=12)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
    doc.save(tmp_path)
    doc.close()
    return Path(tmp_path).read_bytes(), tmp_path


def read_paprikarecipes(path: str) -> list[dict]:
    """Open a .paprikarecipes ZIP and return list of parsed recipe dicts."""
    recipes: list[dict] = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            raw = zf.read(name)
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
            recipes.append(data)
    return recipes
