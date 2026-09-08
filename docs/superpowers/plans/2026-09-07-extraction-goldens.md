# Extraction Goldens Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give RecipeParser a golden-test layer that locks every deterministic step (readers, prompt rendering, schema generation, stage parsing, writers) against a real-input corpus and recorded Gemini replies, running fully offline in CI.

**Architecture:** A new `tests/goldens/` package holds a seven-file real-input corpus, a `GoldenClient` that stands in for `google.genai.Client` at the `client` argument seam (record real replies with `--record-gemini`, replay them offline by default), and four test families that compare against checked-in expected files. No production behaviour changes; the only production edit is a behaviour-preserving refactor pulling inline prompt f-strings in `gemini.py` and `toc.py` into pure builder functions so they can be snapshotted.

**Tech Stack:** Python 3.11/3.12, pytest 9, syrupy 4, pydantic 2, PyMuPDF (`fitz`), ebooklib, BeautifulSoup, google-genai (record mode only).

**Spec:** `docs/extraction-goldens/SPEC-extraction-goldens-design.md`

---

## Global Constraints

- **Base branch:** `feat/extraction-goldens`, branched from `master` at `8fdad38` (the PR #15 merge). Baseline before any change: `578 passed`.
- **Total corpus size:** all files under `tests/goldens/corpus/` must total **under 2 MB**. A build-time assertion enforces this.
- **No network in the test suite.** Replay is the default mode. The only network steps in this plan are (a) the one-time Gutenberg fetch in Task 2 and (b) the deliberate `--record-gemini` runs in Tasks 9 and 10. Neither runs in CI.
- **No production behaviour changes.** Task 7 is a pure refactor: the rendered prompt strings must be byte-identical before and after.
- **Existing tests are never edited or deleted.** `tests/snapshots/`, `tests/unit/`, `tests/test_*.py` and the `tests/live_*.py` scripts stay exactly as they are.
- **`pytest_addoption` must live in `tests/conftest.py`.** Verified empirically on 2026-09-07: pytest silently ignores `pytest_addoption` in a non-initial conftest such as `tests/goldens/conftest.py` — the option is never registered and `config.getoption` raises `ValueError: no option named ...`. This is a deviation from spec §3, which places the flags in `tests/goldens/conftest.py`.
- **`GOOGLE_API_KEY` is always set.** `tests/conftest.py:139` does `os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")`. Record mode must reject that exact sentinel value, not merely check for a non-empty key.
- **CI needs no workflow change.** `.github/workflows/build-installer.yml:68` already runs `python -m pytest tests/ -v`, which picks up `tests/goldens/`.
- **Commit style:** conventional commits, subject in the imperative, one commit per task. End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  ```
- **Run tests from the worktree root**, `C:\Users\iball\Arduino\Projects\RecipeParser\RecipeParser\.claude\worktrees\extraction-goldens`. Commands below use `python -m pytest`.

---

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `tests/goldens/__init__.py` | Marks the package (matches `tests/unit/`, `tests/snapshots/`). |
| `tests/goldens/paths.py` | Directory constants and `corpus_path()` / `golden_path()` helpers. One place that knows the layout. |
| `tests/goldens/conftest.py` | `golden_client` factory fixture, `FIXED_AXES`, `update_goldens` fixture. Consumes the flags registered in `tests/conftest.py`. |
| `tests/goldens/golden_client.py` | `GoldenClient` plus the pure keying functions (`sniff_stage`, `prompt_body`, `body_sha8`). |
| `tests/goldens/corpus/` | The seven fixture files plus `README.md` (provenance and licence per file). |
| `tests/goldens/readers/*.json` | Expected reader output, one per fixture. |
| `tests/goldens/gemini/<fixture>/<body-sha8>/<stage>-<nn>.json` | Recorded Gemini replies. |
| `tests/goldens/e2e/<fixture>/{ingest,paprika,cayenne}.json` | Expected end-to-end output. |
| `tests/goldens/__snapshots__/` | Syrupy files for prompts, schemas, and stage output. |
| `tests/goldens/test_golden_harness.py` | Meta tests: flags registered, paths resolve, corpus/README cross-check. |
| `tests/goldens/test_golden_client.py` | Unit tests for the keying functions and both client modes. |
| `tests/goldens/test_readers_golden.py` | Reader golden family (§6.1). |
| `tests/goldens/test_stages_golden.py` | Stage replay family (§6.2). |
| `tests/goldens/test_prompts_snapshot.py` | Prompt and schema snapshot family (§6.3). |
| `tests/goldens/test_e2e_golden.py` | End-to-end family (§6.4). |
| `tools/build_golden_corpus.py` | Builds every corpus file from checked-in sources plus one Gutenberg download. Not collected by pytest (`python_files = ["test_*.py"]`). |

**Modified**

| Path | Change |
|---|---|
| `tests/conftest.py` | Add `pytest_addoption` registering `--record-gemini` and `--update-goldens`. |
| `recipeparser/gemini.py` | Task 7: extract four prompt builders; call sites use them. |
| `recipeparser/toc.py` | Task 7: extract two prompt builders; call sites use them. |
| `.gitignore` | Nothing to add — corpus and goldens are committed on purpose. Confirm no rule excludes `*.pdf`/`*.epub` (Task 2, Step 1). |

---

## Task 1: Golden harness skeleton

Creates the package, registers the two pytest flags in the only place pytest honours them, and gives every later task a single source of truth for paths and the fixed taxonomy axes.

**Files:**
- Create: `tests/goldens/__init__.py`
- Create: `tests/goldens/paths.py`
- Create: `tests/goldens/conftest.py`
- Create: `tests/goldens/test_golden_harness.py`
- Modify: `tests/conftest.py` (append `pytest_addoption` near the top, before the tkinter stubs)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tests.goldens.paths.GOLDENS_DIR: Path`, `CORPUS_DIR: Path`, `READERS_DIR: Path`, `GEMINI_DIR: Path`, `E2E_DIR: Path`
  - `tests.goldens.paths.corpus_path(name: str) -> Path`
  - `tests.goldens.paths.CORPUS_FIXTURES: tuple[str, ...]` — the seven fixture filenames, in a fixed order
  - `tests.goldens.conftest.FIXED_AXES: dict[str, list[str]]`
  - fixtures `update_goldens: bool`, `record_gemini: bool` (the `golden_client` factory arrives in Task 5)

- [ ] **Step 1: Write the failing test**

Create `tests/goldens/__init__.py` as an empty file, then create `tests/goldens/test_golden_harness.py`:

```python
"""Meta tests for the golden harness itself — no Gemini, no corpus content."""
from __future__ import annotations

from pathlib import Path

from tests.goldens import paths


def test_flags_are_registered_and_default_off(request):
    assert request.config.getoption("--record-gemini") is False
    assert request.config.getoption("--update-goldens") is False


def test_fixture_flags_mirror_the_options(record_gemini, update_goldens):
    assert record_gemini is False
    assert update_goldens is False


def test_paths_point_inside_the_goldens_package():
    assert paths.GOLDENS_DIR.name == "goldens"
    assert paths.CORPUS_DIR == paths.GOLDENS_DIR / "corpus"
    assert paths.READERS_DIR == paths.GOLDENS_DIR / "readers"
    assert paths.GEMINI_DIR == paths.GOLDENS_DIR / "gemini"
    assert paths.E2E_DIR == paths.GOLDENS_DIR / "e2e"


def test_corpus_path_joins_onto_the_corpus_dir():
    assert paths.corpus_path("dual-units.epub") == paths.CORPUS_DIR / "dual-units.epub"


def test_seven_fixtures_are_declared():
    assert len(paths.CORPUS_FIXTURES) == 7
    assert len(set(paths.CORPUS_FIXTURES)) == 7


def test_fixed_axes_are_the_two_axes_the_spec_names():
    from tests.goldens.conftest import FIXED_AXES

    assert sorted(FIXED_AXES) == ["Cuisine", "Meal Type"]
    assert all(isinstance(v, list) and v for v in FIXED_AXES.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/goldens/test_golden_harness.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'tests.goldens.paths'`.

- [ ] **Step 3: Register the flags in `tests/conftest.py`**

Insert this immediately after the module docstring in `tests/conftest.py` (before the tkinter stub section):

```python
def pytest_addoption(parser):
    """Register the golden-suite flags.

    These MUST live here, not in tests/goldens/conftest.py: pytest only honours
    pytest_addoption in an *initial* conftest (one under rootdir or a testpath).
    A nested conftest's hook is silently ignored and config.getoption then
    raises ValueError.
    """
    parser.addoption(
        "--record-gemini",
        action="store_true",
        default=False,
        help="Call the real Gemini API and record replies under tests/goldens/gemini/.",
    )
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Rewrite expected files under tests/goldens/readers/ and tests/goldens/e2e/.",
    )
```

- [ ] **Step 4: Write `tests/goldens/paths.py`**

```python
"""Filesystem layout of the golden suite — the one place that knows it."""
from __future__ import annotations

from pathlib import Path

GOLDENS_DIR: Path = Path(__file__).resolve().parent
CORPUS_DIR: Path = GOLDENS_DIR / "corpus"
READERS_DIR: Path = GOLDENS_DIR / "readers"
GEMINI_DIR: Path = GOLDENS_DIR / "gemini"
E2E_DIR: Path = GOLDENS_DIR / "e2e"

#: Every corpus file, in the order the spec's table lists them.  A fixture id
#: is its filename: it is what the reader is handed and what names its golden.
CORPUS_FIXTURES: tuple[str, ...] = (
    "gutenberg-multi.epub",
    "dual-units.epub",
    "phases-bakers.epub",
    "text-pages.pdf",
    "scanned.pdf",
    "saved-page.html",
    "legacy-photo.paprikarecipes",
)


def corpus_path(name: str) -> Path:
    """Absolute path to one corpus file."""
    return CORPUS_DIR / name


def golden_path(kind: str, name: str) -> Path:
    """Absolute path to an expected-output file, e.g. golden_path("readers", "scanned.pdf")."""
    return GOLDENS_DIR / kind / f"{name}.json"
```

- [ ] **Step 5: Write `tests/goldens/conftest.py`**

```python
"""Fixtures shared by the golden test families."""
from __future__ import annotations

from typing import Dict, List

import pytest

#: The taxonomy handed to every refine call in the golden suite.  Fixed here so
#: a recorded reply keys against a prompt that cannot drift with the user's
#: real categories.yaml.
FIXED_AXES: Dict[str, List[str]] = {
    "Cuisine": ["American", "British", "French", "Italian"],
    "Meal Type": ["Breakfast", "Dessert", "Dinner", "Bread"],
}


@pytest.fixture
def record_gemini(request) -> bool:
    """True when the run was started with --record-gemini."""
    return bool(request.config.getoption("--record-gemini"))


@pytest.fixture
def update_goldens(request) -> bool:
    """True when the run was started with --update-goldens."""
    return bool(request.config.getoption("--update-goldens"))
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/goldens -v`
Expected: 6 passed.

- [ ] **Step 7: Verify nothing else moved**

Run: `python -m pytest -q`
Expected: `584 passed` (578 baseline + 6 new).

- [ ] **Step 8: Commit**

```bash
git add tests/goldens/__init__.py tests/goldens/paths.py tests/goldens/conftest.py tests/goldens/test_golden_harness.py tests/conftest.py
git commit -m "test(goldens): a home for goldens, and the flags that drive them"
```

---

## Task 2: The corpus

Builds all seven fixtures from a build script plus one Project Gutenberg download, writes `corpus/README.md`, and adds the cross-check test that fails when a corpus file has no README entry (spec §8).

The hand-rebuilt excerpts are authored in this plan and live as string constants inside the build script, so `corpus/` contains only fixtures and the README. Each keeps layout features only, carries no author or book name, and is at most two recipes long.

**Files:**
- Create: `tools/build_golden_corpus.py`
- Create: `tests/goldens/corpus/README.md`
- Create (built, committed): the seven files listed in `CORPUS_FIXTURES`
- Modify: `tests/goldens/test_golden_harness.py` (add the cross-check test)

**Interfaces:**
- Consumes: `tests.goldens.paths.CORPUS_DIR`, `CORPUS_FIXTURES`.
- Produces: the corpus files themselves, and a `corpus/README.md` whose markdown table's first column is a backtick-quoted filename.

- [ ] **Step 1: Confirm nothing gitignores the corpus**

Run:
```bash
git check-ignore -v tests/goldens/corpus/x.epub tests/goldens/corpus/x.pdf tests/goldens/corpus/x.paprikarecipes; echo "exit=$?"
```
Expected: no output and `exit=1` (nothing matched). If any rule matches, add a negation to `.gitignore` before continuing.

- [ ] **Step 2: Write the failing cross-check test**

Append to `tests/goldens/test_golden_harness.py`:

```python
def _readme_fixture_names() -> set[str]:
    """Filenames in the first column of the README's provenance table."""
    import re

    text = (paths.CORPUS_DIR / "README.md").read_text(encoding="utf-8")
    names: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        first = line.split("|")[1].strip()
        m = re.fullmatch(r"`([^`]+)`", first)
        if m:
            names.add(m.group(1))
    return names


def test_every_corpus_file_has_a_readme_entry():
    on_disk = {p.name for p in paths.CORPUS_DIR.iterdir() if p.name != "README.md"}
    assert on_disk == _readme_fixture_names()


def test_the_readme_documents_exactly_the_declared_fixtures():
    assert _readme_fixture_names() == set(paths.CORPUS_FIXTURES)


def test_the_corpus_stays_under_two_megabytes():
    total = sum(p.stat().st_size for p in paths.CORPUS_DIR.iterdir())
    assert total < 2 * 1024 * 1024, f"corpus is {total} bytes"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/goldens/test_golden_harness.py -v`
Expected: FAIL — `FileNotFoundError` on `corpus/README.md`.

- [ ] **Step 4: Write the build script**

Create `tools/build_golden_corpus.py`. It has one subcommand per fixture group and a `--all` mode. The hand-rebuilt sources are constants at the top.

```python
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
    import fitz

    doc = fitz.open()
    for index, body in enumerate(pages[:4]):
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(fitz.Rect(54, 54, 558, 738), body, fontsize=10, fontname="helv")
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
        params={"topic": "cookery", "languages": "en"},
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
            new.content = item.get_content().decode("utf-8", "replace")
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
```

- [ ] **Step 5: Build the corpus**

Run: `python tools/build_golden_corpus.py --all`
Expected: the Gutenberg provenance JSON printed, then `corpus total: <n> bytes` with `n < 2097152`. Record the printed provenance — Step 6 needs it.

If the fetch fails or the printed size is over budget, reduce `docs[:7]` to fewer parts and rerun. Do not proceed until the size assertion passes.

- [ ] **Step 6: Write `tests/goldens/corpus/README.md`**

Use the provenance printed in Step 5 to fill the Gutenberg row's id, title, author and licence. Every other row is fixed text:

```markdown
# Golden corpus

Real inputs for `tests/goldens/`. Every file here must appear in the table
below — `test_every_corpus_file_has_a_readme_entry` fails otherwise.

Rebuild with `python tools/build_golden_corpus.py --all` (needs network once,
for the Gutenberg download). The built files are committed; the script exists
so a reviewer can see how each was made.

| File | Source and licence | What was trimmed or rebuilt | Hard case it covers |
|---|---|---|---|
| `gutenberg-multi.epub` | Project Gutenberg ebook #<ID>, "<TITLE>" by <AUTHORS>. Public domain in the US; Project Gutenberg licence. | First seven documents kept; nav and NCX regenerated by ebooklib during the trim, so the TOC is real rather than a stub. | Real nav/NCX TOC; front matter the candidate filter must reject; several recipes per chapter |
| `dual-units.epub` | Hand-rebuilt excerpt, two recipes. No author, no book name. | Written from scratch to carry only the layout features. | Dual-unit lines such as "2 cups/250g"; hero image before the title; hero image after the ingredient list |
| `phases-bakers.epub` | Hand-rebuilt excerpt, two recipes. No author, no book name. | Written from scratch to carry only the layout features. | Multi-phase bread recipe; baker's percentage table that triggers `needs_table_normalisation` |
| `text-pages.pdf` | Built in code from the trimmed Gutenberg text above; same licence. | Four pages, one embedded 1x1 JPEG. | PDF preflight pass; page chunks with image markers; PDF text layer |
| `scanned.pdf` | Built in code by rendering the first two pages of `text-pages.pdf` to images; same licence. | Image-only; no text layer at all. | Preflight failure; vision OCR fallback |
| `saved-page.html` | Hand-rebuilt recipe page. No real site, no attribution. | Written from scratch: nav, article, ingredients, directions, footer. | `UrlReader` with `requests.get` patched; `utils.html_to_text` |
| `legacy-photo.paprikarecipes` | Built in code from one Gutenberg recipe plus a 1x1 JPEG; same licence. | One entry, no `_cayenne_meta`. | Paprika legacy reader with `photo_data`, which PR #13 touches |

## Regenerating expected output

- `pytest tests/goldens --update-goldens` rewrites `readers/` and `e2e/`.
- `pytest tests/goldens --snapshot-update` rewrites `__snapshots__/`.
- `pytest tests/goldens --record-gemini` re-records `gemini/`. It needs a real
  `GOOGLE_API_KEY`, costs money, and never runs in CI. Do it only when a prompt
  or model changes on purpose, and commit the prompt, the recordings and the
  resulting output together.
```

- [ ] **Step 7: Run the cross-check tests to verify they pass**

Run: `python -m pytest tests/goldens -v`
Expected: 9 passed.

- [ ] **Step 8: Sanity-check the fixtures do what they claim**

Run:
```bash
python -c "
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.pdf import PdfReader
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.gemini import needs_table_normalisation
from recipeparser.exceptions import PdfExtractionError
from tests.goldens.paths import corpus_path
for n in ('gutenberg-multi.epub','dual-units.epub','phases-bakers.epub'):
    cs = EpubReader().read(str(corpus_path(n)))
    print(n, len(cs), 'chunks', 'table=', any(needs_table_normalisation(c.text) for c in cs))
print('text-pages.pdf', len(PdfReader().read(str(corpus_path('text-pages.pdf')))), 'chunks')
try:
    PdfReader().read(str(corpus_path('scanned.pdf')))
    print('scanned.pdf DID NOT RAISE - fixture is wrong')
except PdfExtractionError as e:
    print('scanned.pdf raised:', str(e)[:60])
cs = PaprikaReader().read(str(corpus_path('legacy-photo.paprikarecipes')))
print('paprika', len(cs), 'image_bytes=', bool(cs[0].image_bytes))
"
```
Expected: every EPUB yields at least one chunk; `phases-bakers.epub` reports `table= True`; `text-pages.pdf` yields chunks; `scanned.pdf` raises with a message starting `PDF has little or no extractable text`; the Paprika chunk reports `image_bytes= True`.

If `phases-bakers.epub` reports `table= False`, the baker's table did not survive the EPUB round-trip — adjust `PHASES_BAKERS_CHAPTERS` so the literal string `Baker's %` reaches the chunk text, rebuild, and rerun.

- [ ] **Step 9: Commit**

```bash
git add tools/build_golden_corpus.py tests/goldens/corpus tests/goldens/test_golden_harness.py
git commit -m "test(goldens): seven real inputs, and where each one came from"
```

---

## Task 3: GoldenClient keying

The pure functions that decide which file a call belongs to. Split from the client itself because the keying is the part with real logic and it is worth its own test cycle.

**Files:**
- Create: `tests/goldens/golden_client.py` (keying functions only)
- Create: `tests/goldens/test_golden_client.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class UnknownPromptError(RuntimeError)`
  - `text_part(contents: object) -> str` — the text of a `str` or of a genai `contents` list
  - `sniff_stage(contents: object) -> str` — one of `extract`, `refine`, `table`, `toc-parse`, `toc-classify`, `vision`, `connectivity`; raises `UnknownPromptError`
  - `prompt_body(contents: object, stage: str) -> str`
  - `body_sha8(body: str) -> str`
  - `record_key(stage: str, body: str, ordinal: int) -> tuple[str, str]` returning `(body_sha8, "<stage>-<nn>.json")`

- [ ] **Step 1: Write the failing test**

Create `tests/goldens/test_golden_client.py`:

```python
"""Unit tests for the GoldenClient keying rules (spec §5.2)."""
from __future__ import annotations

import pytest

from recipeparser import gemini, toc
from recipeparser.models import RecipeExtraction
from tests.goldens import golden_client as gc
from tests.goldens.conftest import FIXED_AXES


def _sent_prompt(monkeypatch, call) -> object:
    """Run *call* against a client that captures contents and returns nothing useful."""
    seen = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen["contents"] = contents
            raise RuntimeError("stop here — we only wanted the prompt")

    class _Client:
        models = _Models()

    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)
    try:
        call(_Client())
    except Exception:
        pass
    return seen["contents"]


class TestSniffStage:
    def test_book_extract_prompt_sniffs_as_extract(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch, lambda c: gemini.extract_recipes("BAKER'S % table inside", c)
        )
        assert gc.sniff_stage(contents) == "extract"

    def test_plain_text_extract_prompt_sniffs_as_extract(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch, lambda c: gemini.extract_recipe_from_text("a recipe", c)
        )
        assert gc.sniff_stage(contents) == "extract"

    def test_refine_prompt_sniffs_as_refine(self, monkeypatch):
        raw = RecipeExtraction(name="X", ingredients=["1 cup flour"], directions=["Mix."])
        contents = _sent_prompt(
            monkeypatch,
            lambda c: gemini.refine_recipe_for_cayenne(raw, c, user_axes=FIXED_AXES),
        )
        assert gc.sniff_stage(contents) == "refine"

    def test_table_prompt_sniffs_as_table(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch, lambda c: gemini.normalise_baker_table("Flour 500g 100%", c)
        )
        assert gc.sniff_stage(contents) == "table"

    def test_toc_parse_prompt_sniffs_as_toc_parse(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch, lambda c: toc._parse_toc_from_text_fallback(["Contents", "Soups 3"], c)
        )
        assert gc.sniff_stage(contents) == "toc-parse"

    def test_toc_classify_prompt_sniffs_as_toc_classify(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch,
            lambda c: toc._classify_toc_recipe_indices([("Baker's % Bread", 3)], c),
        )
        assert gc.sniff_stage(contents) == "toc-classify"

    def test_connectivity_prompt_sniffs_as_connectivity(self):
        assert gc.sniff_stage("Reply with the single word OK.") == "connectivity"

    def test_vision_prompt_sniffs_as_vision(self):
        contents = [object(), "You are an OCR assistant. The image is a page from a recipe document."]
        assert gc.sniff_stage(contents) == "vision"

    def test_an_unrecognised_prompt_fails_loudly(self):
        with pytest.raises(gc.UnknownPromptError):
            gc.sniff_stage("Write me a poem about soup.")


class TestPromptBody:
    def test_extract_body_is_the_chunk_text_only(self, monkeypatch):
        contents = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        assert gc.prompt_body(contents, "extract").strip() == "MY CHUNK"

    def test_the_units_mode_does_not_change_the_body(self, monkeypatch):
        book = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c, units="book"))
        metric = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c, units="metric"))
        assert gc.prompt_body(book, "extract") == gc.prompt_body(metric, "extract")

    def test_table_body_is_the_chunk_text_only(self, monkeypatch):
        contents = _sent_prompt(monkeypatch, lambda c: gemini.normalise_baker_table("MY TABLE", c))
        assert gc.prompt_body(contents, "table").strip() == "MY TABLE"

    def test_refine_body_is_the_raw_recipe_repr(self, monkeypatch):
        raw = RecipeExtraction(name="X", ingredients=["1 cup flour"], directions=["Mix."])
        contents = _sent_prompt(
            monkeypatch, lambda c: gemini.refine_recipe_for_cayenne(raw, c, user_axes=FIXED_AXES)
        )
        assert gc.prompt_body(contents, "refine").strip() == str(raw)

    def test_stages_without_a_marker_use_the_whole_prompt(self):
        assert gc.prompt_body("Reply with the single word OK.", "connectivity") == (
            "Reply with the single word OK."
        )


class TestKeys:
    def test_body_sha8_is_eight_stable_hex_digits(self):
        first = gc.body_sha8("some chunk")
        assert first == gc.body_sha8("some chunk")
        assert len(first) == 8 and all(ch in "0123456789abcdef" for ch in first)

    def test_different_bodies_key_differently(self):
        assert gc.body_sha8("chunk a") != gc.body_sha8("chunk b")

    def test_record_key_pairs_the_directory_with_a_zero_padded_filename(self):
        directory, filename = gc.record_key("extract", "chunk", 0)
        assert directory == gc.body_sha8("chunk")
        assert filename == "extract-00.json"

    def test_the_parse_retry_lands_on_the_next_ordinal(self):
        assert gc.record_key("extract", "chunk", 1)[1] == "extract-01.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/goldens/test_golden_client.py -v`
Expected: collection error — `cannot import name 'golden_client'`.

- [ ] **Step 3: Write the keying functions**

Create `tests/goldens/golden_client.py`:

```python
"""A stand-in for google.genai.Client that records real replies and replays them.

Keying (spec §5.2) is the interesting part and lives in the pure functions at
the top of this module: which recorded file a call belongs to is decided by the
identity of the work unit in the prompt, never by a global call counter, so
replay is stable at any thread-pool size.
"""
from __future__ import annotations

import hashlib
from typing import Iterable, Tuple


class UnknownPromptError(RuntimeError):
    """A prompt reached the golden client that no stage rule recognises."""


#: Ordered (stage, marker) rules matched against the prompt head.  Order matters:
#: `table` is last because its marker is the only one that a *chunk body* can
#: plausibly contain, and every rule above it anchors at the start of its own
#: prompt.
_STAGE_RULES: Tuple[Tuple[str, str], ...] = (
    ("refine", "culinary data refiner"),
    ("extract", "culinary data extractor"),
    ("toc-parse", "table-of-contents or contents page"),
    ("toc-classify", "table-of-contents entries from a cookbook"),
    ("vision", "ocr assistant"),
    ("connectivity", "reply with the single word ok"),
    ("table", "baker"),
)

#: Where each stage's body starts.  An empty tuple means "the whole prompt is
#: the body" — vision, both TOC calls, and the connectivity ping.
_BODY_MARKERS = {
    "extract": ("Text chunk:", "Text:"),
    "table": ("Text:",),
    "refine": ("RAW RECIPE:",),
    "toc-parse": (),
    "toc-classify": (),
    "vision": (),
    "connectivity": (),
}

#: How much of the prompt the stage sniff reads.  Long enough to reach the
#: table prompt's marker on its second line, short enough that an embedded
#: chunk body cannot reach it.
_HEAD_CHARS = 600


def text_part(contents: object) -> str:
    """The text of a genai ``contents`` argument.

    ``gemini.py`` passes a plain string everywhere except the vision call, which
    passes ``[Part.from_bytes(...), PROMPT]``.  Image bytes are corpus files, so
    only the text matters for keying.
    """
    if isinstance(contents, str):
        return contents
    if isinstance(contents, Iterable):
        parts = [p for p in contents if isinstance(p, str)]
        if parts:
            return "\n".join(parts)
    raise UnknownPromptError(f"contents carried no text part: {type(contents)!r}")


def sniff_stage(contents: object) -> str:
    """Which pipeline stage sent this prompt."""
    head = text_part(contents).strip()[:_HEAD_CHARS].lower()
    for stage, marker in _STAGE_RULES:
        if marker in head:
            return stage
    raise UnknownPromptError(
        "no stage rule matched this prompt. First 200 chars:\n"
        + text_part(contents).strip()[:200]
    )


def prompt_body(contents: object, stage: str) -> str:
    """The work unit inside the prompt: everything after the stage's marker."""
    text = text_part(contents)
    for marker in _BODY_MARKERS.get(stage, ()):
        index = text.find(marker)
        if index != -1:
            return text[index + len(marker):]
    return text


def body_sha8(body: str) -> str:
    """First eight hex digits of the SHA-256 of a prompt body."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def record_key(stage: str, body: str, ordinal: int) -> Tuple[str, str]:
    """The (directory, filename) a call's recording lives under."""
    return body_sha8(body), f"{stage}-{ordinal:02d}.json"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/goldens/test_golden_client.py -v`
Expected: all passed.

If `test_table_prompt_sniffs_as_table` fails, print the table prompt head and confirm `baker` falls inside the first 600 characters; raise `_HEAD_CHARS` only as far as needed and re-run `test_book_extract_prompt_sniffs_as_extract`, which guards the other direction.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: no failures; count rises by the new tests only.

- [ ] **Step 6: Commit**

```bash
git add tests/goldens/golden_client.py tests/goldens/test_golden_client.py
git commit -m "test(goldens): key a recording by the work unit, not a call counter"
```

---

## Task 4: GoldenClient record and replay

The client itself, plus the `golden_client` factory fixture.

**Files:**
- Modify: `tests/goldens/golden_client.py` (append the client)
- Modify: `tests/goldens/conftest.py` (add the fixture)
- Modify: `tests/goldens/test_golden_client.py` (append client tests)

**Interfaces:**
- Consumes: `sniff_stage`, `prompt_body`, `record_key` from Task 3; `paths.GEMINI_DIR`.
- Produces:
  - `class GoldenResponse` with attributes `text: str`, `parsed: object | None`, `candidates: tuple`
  - `class GoldenClient(fixture_id: str, root: Path, record: bool = False)` exposing `.models.generate_content(*, model, contents, config)` and `.models.embed_content(*, model, contents, config)`
  - `class MissingRecordingError(AssertionError)`
  - conftest fixture `golden_client` — a callable `(fixture_id: str) -> GoldenClient`

- [ ] **Step 1: Write the failing test**

Append to `tests/goldens/test_golden_client.py`:

```python
import json
import warnings

from tests.goldens.golden_client import GoldenClient, MissingRecordingError


def _write_recording(root, fixture, stage, body, ordinal, response_text, sha=None):
    directory, filename = gc.record_key(stage, body, ordinal)
    target = root / fixture / directory
    target.mkdir(parents=True, exist_ok=True)
    target.joinpath(filename).write_text(
        json.dumps(
            {
                "stage": stage,
                "ordinal": ordinal,
                "model": "gemini-2.5-flash",
                "config": {"temperature": 0.1},
                "prompt_sha256": sha or "0" * 64,
                "response_text": response_text,
            }
        ),
        encoding="utf-8",
    )


class TestReplay:
    def test_it_serves_the_recorded_reply(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        body = gc.prompt_body(prompt, "extract")
        _write_recording(tmp_path, "f.epub", "extract", body, 0, '{"recipes": []}')

        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt, config={}
        )
        assert response.text == '{"recipes": []}'

    def test_a_second_call_for_one_body_serves_the_next_ordinal(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        body = gc.prompt_body(prompt, "extract")
        _write_recording(tmp_path, "f.epub", "extract", body, 0, "truncated {")
        _write_recording(tmp_path, "f.epub", "extract", body, 1, '{"recipes": []}')

        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        first = client.models.generate_content(model="m", contents=prompt, config={})
        second = client.models.generate_content(model="m", contents=prompt, config={})
        assert first.text == "truncated {"
        assert second.text == '{"recipes": []}'

    def test_a_missing_recording_names_the_path_it_wanted(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        with pytest.raises(MissingRecordingError) as excinfo:
            client.models.generate_content(model="m", contents=prompt, config={})
        assert "extract-00.json" in str(excinfo.value)

    def test_a_prompt_hash_mismatch_warns_but_still_serves(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        body = gc.prompt_body(prompt, "extract")
        _write_recording(tmp_path, "f.epub", "extract", body, 0, "ok", sha="f" * 64)

        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            response = client.models.generate_content(model="m", contents=prompt, config={})
        assert response.text == "ok"
        assert any("prompt_sha256" in str(w.message) for w in caught)

    def test_the_parse_retry_replays_end_to_end(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        body = gc.prompt_body(prompt, "extract")
        _write_recording(tmp_path, "f.epub", "extract", body, 0, "{ truncated")
        _write_recording(
            tmp_path, "f.epub", "extract", body, 1,
            '{"recipes": [{"name": "Scones", "ingredients": ["1 cup flour"], "directions": ["Bake."]}]}',
        )
        monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)

        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        result = gemini.extract_recipes("MY CHUNK", client)
        assert [r.name for r in result.recipes] == ["Scones"]


class TestEmbeddings:
    def test_it_returns_a_deterministic_1536_vector(self, tmp_path):
        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        first = client.models.embed_content(model="m", contents="soup", config=None)
        second = client.models.embed_content(model="m", contents="soup", config=None)
        assert len(first.embeddings[0].values) == 1536
        assert first.embeddings[0].values == second.embeddings[0].values

    def test_different_text_gives_a_different_vector(self, tmp_path):
        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        soup = client.models.embed_content(model="m", contents="soup", config=None)
        cake = client.models.embed_content(model="m", contents="cake", config=None)
        assert soup.embeddings[0].values != cake.embeddings[0].values

    def test_it_never_touches_the_recordings(self, tmp_path):
        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        client.models.embed_content(model="m", contents="soup", config=None)
        assert not (tmp_path / "f.epub").exists()


class TestRecordModeGuard:
    def test_the_dummy_key_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            GoldenClient(fixture_id="f.epub", root=tmp_path, record=True)

    def test_an_absent_key_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            GoldenClient(fixture_id="f.epub", root=tmp_path, record=True)


def test_the_fixture_hands_back_a_replay_client(golden_client, tmp_path):
    client = golden_client("dual-units.epub")
    assert client.fixture_id == "dual-units.epub"
    assert client.record is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/goldens/test_golden_client.py -v`
Expected: `ImportError: cannot import name 'GoldenClient'`.

- [ ] **Step 3: Append the client to `tests/goldens/golden_client.py`**

```python
import json
import logging
import os
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: Config keys not worth storing.  The response schema is large and is covered
#: by its own snapshot (spec §6.3).
_CONFIG_SKIP = {"response_json_schema", "response_schema"}

_EMBEDDING_DIMS = 1536


class MissingRecordingError(AssertionError):
    """Replay was asked for a reply that was never recorded."""


@dataclass
class GoldenEmbedding:
    values: List[float]


@dataclass
class GoldenEmbedResponse:
    embeddings: List[GoldenEmbedding]


@dataclass
class GoldenResponse:
    """The slice of a genai response the production code actually reads.

    ``gemini.py`` reads ``.text`` and, on the unparseable path, ``.candidates``.
    ``toc.py`` reads ``.parsed``; replaying TOC calls is out of scope here, so
    ``parsed`` stays None and the TOC helpers fall through to their own
    "AI parsing failed" branch rather than returning wrong data.
    """

    text: str
    parsed: Optional[object] = None
    candidates: tuple = field(default_factory=tuple)


def _seeded_vector(text: str) -> List[float]:
    """A deterministic unit-ish vector derived from *text*.

    Embeddings are never recorded: the embed stage has no parse logic worth
    locking and 1536 floats per recipe would bloat the fixtures.  The assemble
    stage still receives a real-length vector.
    """
    import random

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest, "big"))
    return [rng.uniform(-1.0, 1.0) for _ in range(_EMBEDDING_DIMS)]


class _GoldenModels:
    """The ``client.models`` surface the production code calls."""

    def __init__(self, owner: "GoldenClient") -> None:
        self._owner = owner

    def generate_content(self, *, model: str, contents: object, config: dict) -> GoldenResponse:
        return self._owner._generate(model=model, contents=contents, config=config)

    def embed_content(self, *, model: str, contents: str, config: object = None) -> GoldenEmbedResponse:
        return GoldenEmbedResponse(embeddings=[GoldenEmbedding(values=_seeded_vector(contents))])


class GoldenClient:
    """Stands in for ``google.genai.Client`` for one corpus fixture.

    Replay (the default) serves files under ``<root>/<fixture_id>/`` and never
    touches the network.  Record mode forwards to a real client and writes each
    reply before returning it.
    """

    def __init__(self, fixture_id: str, root: Path, record: bool = False) -> None:
        self.fixture_id = fixture_id
        self.root = Path(root)
        self.record = bool(record)
        self.models = _GoldenModels(self)
        self._ordinals: Dict[tuple, int] = {}
        self._lock = threading.Lock()
        self._real: Any = None

        if self.record:
            key = os.environ.get("GOOGLE_API_KEY", "").strip()
            if not key or key == "dummy-key-for-tests":
                raise RuntimeError(
                    "--record-gemini needs a real GOOGLE_API_KEY. The test suite sets "
                    "GOOGLE_API_KEY=dummy-key-for-tests by default (tests/conftest.py), so "
                    "export a real key in this shell before recording."
                )
            from google import genai

            self._real = genai.Client(api_key=key)

    # ── internals ────────────────────────────────────────────────────────────

    @property
    def _fixture_dir(self) -> Path:
        return self.root / self.fixture_id

    def _next_ordinal(self, stage: str, body: str) -> int:
        """The next per-(body, stage) ordinal.

        Every call for one body happens on one worker thread in a fixed order,
        so this counter is only ever advanced by that thread; the lock guards
        the dict, not an ordering assumption.
        """
        with self._lock:
            key = (body_sha8(body), stage)
            ordinal = self._ordinals.get(key, 0)
            self._ordinals[key] = ordinal + 1
            return ordinal

    def _generate(self, *, model: str, contents: object, config: dict) -> GoldenResponse:
        stage = sniff_stage(contents)
        body = prompt_body(contents, stage)
        ordinal = self._next_ordinal(stage, body)
        directory, filename = record_key(stage, body, ordinal)
        path = self._fixture_dir / directory / filename

        if self.record:
            response = self._real.models.generate_content(
                model=model, contents=contents, config=config
            )
            text = getattr(response, "text", "") or ""
            self._write(path, stage, ordinal, model, config, contents, text)
            return GoldenResponse(text=text)

        if not path.exists():
            raise MissingRecordingError(
                f"No recorded Gemini reply at {path}.\n"
                f"stage={stage} ordinal={ordinal} fixture={self.fixture_id}\n"
                "Re-record with: pytest tests/goldens --record-gemini (needs a real GOOGLE_API_KEY)."
            )

        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = payload.get("prompt_sha256")
        actual = hashlib.sha256(text_part(contents).encode("utf-8")).hexdigest()
        if expected and expected != actual:
            warnings.warn(
                f"prompt_sha256 mismatch for {path}: the prompt has drifted since this reply "
                "was recorded. Serving it anyway — the prompt snapshot is what guards drift.",
                stacklevel=2,
            )
        return GoldenResponse(text=payload["response_text"])

    def _write(self, path: Path, stage: str, ordinal: int, model: str,
               config: dict, contents: object, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = {k: v for k, v in (config or {}).items() if k not in _CONFIG_SKIP}
        payload = {
            "stage": stage,
            "ordinal": ordinal,
            "model": model,
            "config": stored,
            "prompt_sha256": hashlib.sha256(text_part(contents).encode("utf-8")).hexdigest(),
            "response_text": text,
        }
        with self._lock:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log.info("recorded %s", path)
```

Move the `import hashlib` at the top of the file above the new imports so the module has a single import block.

- [ ] **Step 4: Add the fixture to `tests/goldens/conftest.py`**

```python
from pathlib import Path
from typing import Callable

from tests.goldens.golden_client import GoldenClient
from tests.goldens.paths import GEMINI_DIR


@pytest.fixture
def golden_client(request) -> Callable[[str], GoldenClient]:
    """Factory: golden_client("dual-units.epub") -> GoldenClient for that fixture."""
    record = bool(request.config.getoption("--record-gemini"))

    def _make(fixture_id: str, root: Path = GEMINI_DIR) -> GoldenClient:
        return GoldenClient(fixture_id=fixture_id, root=root, record=record)

    return _make
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/goldens/test_golden_client.py -v`
Expected: all passed.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -q`
Expected: no failures.

- [ ] **Step 7: Commit**

```bash
git add tests/goldens/golden_client.py tests/goldens/conftest.py tests/goldens/test_golden_client.py
git commit -m "test(goldens): record real replies once, replay them forever"
```

---

## Task 5: Reader goldens

Locks every reader's output against the corpus. No Gemini calls in this family (spec §6.1).

Two deliberate extensions to the spec's serialisation shape, both forced by the data: raw `image_bytes` cannot go in JSON (stored as SHA-256 plus length — and that field is the whole point of the `legacy-photo` fixture, which is what PR #13 touched), and the scanned PDF's preflight message embeds an absolute path (only its stable prefix is stored).

**Files:**
- Create: `tests/goldens/test_readers_golden.py`
- Create: `tests/goldens/readers/*.json` (generated by the first `--update-goldens` run, then committed)

**Interfaces:**
- Consumes: `paths.corpus_path`, `paths.CORPUS_FIXTURES`, the `update_goldens` fixture.
- Produces: nothing later tasks import; Task 6 re-reads the same corpus through the same readers.

- [ ] **Step 1: Write the test**

Create `tests/goldens/test_readers_golden.py`:

```python
"""Reader goldens (spec §6.1) — real inputs against checked-in expected output.

No Gemini calls happen here.  Rewrite the expected files with:
    pytest tests/goldens/test_readers_golden.py --update-goldens
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.core.models import Chunk
from recipeparser.exceptions import PdfExtractionError
from recipeparser.io.readers.epub import EpubReader, load_epub
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.readers.pdf import PdfReader, load_pdf
from recipeparser.io.readers.url import UrlReader
from recipeparser.utils import html_to_text
from tests.goldens.paths import READERS_DIR, corpus_path

EPUB_FIXTURES = ("gutenberg-multi.epub", "dual-units.epub", "phases-bakers.epub")


def _chunk_to_dict(chunk: Chunk) -> dict:
    """Everything about a Chunk that JSON can hold and a regression could break."""
    return {
        "input_type": chunk.input_type.value,
        "source_url": chunk.source_url,
        "image_url": chunk.image_url,
        "image_bytes_sha256": (
            hashlib.sha256(chunk.image_bytes).hexdigest() if chunk.image_bytes else None
        ),
        "image_bytes_len": len(chunk.image_bytes) if chunk.image_bytes else 0,
        "image_content_type": chunk.image_content_type,
        "label": chunk.label,
        "pre_parsed_title": getattr(chunk.pre_parsed, "title", None),
        "pre_parsed_embedding_len": (
            None if chunk.pre_parsed_embedding is None else len(chunk.pre_parsed_embedding)
        ),
        "text": chunk.text,
    }


def _assert_golden(name: str, actual: dict, update: bool) -> None:
    path = READERS_DIR / f"{name}.json"
    rendered = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), (
        f"No reader golden at {path}. Create it with:\n"
        "    pytest tests/goldens/test_readers_golden.py --update-goldens"
    )
    assert json.loads(rendered) == json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", EPUB_FIXTURES)
def test_epub_reader_golden(fixture, tmp_path, update_goldens):
    source = str(corpus_path(fixture))
    chunks = EpubReader().read(source)
    _, _, qualifying, _ = load_epub(source, str(tmp_path))
    _assert_golden(
        fixture,
        {
            "chunks": [_chunk_to_dict(c) for c in chunks],
            "qualifying_images": sorted(qualifying),
        },
        update_goldens,
    )


def test_pdf_reader_golden(tmp_path, update_goldens):
    source = str(corpus_path("text-pages.pdf"))
    chunks = PdfReader().read(source)
    _, _, qualifying, _ = load_pdf(source, str(tmp_path))
    _assert_golden(
        "text-pages.pdf",
        {
            "chunks": [_chunk_to_dict(c) for c in chunks],
            "qualifying_images": sorted(qualifying),
        },
        update_goldens,
    )


def test_scanned_pdf_fails_preflight_golden(update_goldens):
    """OCR belongs to the stage layer; the reader's job here is to refuse."""
    with pytest.raises(PdfExtractionError) as excinfo:
        PdfReader().read(str(corpus_path("scanned.pdf")))
    _assert_golden(
        "scanned.pdf",
        {
            "error": "PdfExtractionError",
            "message_prefix": str(excinfo.value)[:48],
        },
        update_goldens,
    )


def test_url_reader_golden(update_goldens):
    """The saved page stands in for what r.jina.ai would return."""
    html = corpus_path("saved-page.html").read_text(encoding="utf-8")
    response = MagicMock()
    response.text = html
    response.raise_for_status = MagicMock()

    with patch("recipeparser.io.readers.url.requests.get", return_value=response) as get:
        chunks = UrlReader().read("https://example.invalid/tomato-soup")

    get.assert_called_once()
    assert get.call_args.args[0] == "https://r.jina.ai/https://example.invalid/tomato-soup"
    _assert_golden(
        "saved-page.html",
        {"chunks": [_chunk_to_dict(c) for c in chunks], "qualifying_images": []},
        update_goldens,
    )


def test_html_to_text_golden(update_goldens):
    """The HTML-to-text path the API adapter uses, against the same saved page."""
    html = corpus_path("saved-page.html").read_text(encoding="utf-8")
    _assert_golden(
        "saved-page.html-to-text",
        {"text": html_to_text(html)},
        update_goldens,
    )


def test_paprika_reader_golden(update_goldens):
    chunks = PaprikaReader().read(str(corpus_path("legacy-photo.paprikarecipes")))
    _assert_golden(
        "legacy-photo.paprikarecipes",
        {"chunks": [_chunk_to_dict(c) for c in chunks], "qualifying_images": []},
        update_goldens,
    )


def test_the_legacy_photo_fixture_really_carries_a_photo():
    """Guards the fixture itself: PR #13 is the reason photo_data is in the corpus."""
    chunk = PaprikaReader().read(str(corpus_path("legacy-photo.paprikarecipes")))[0]
    assert chunk.image_bytes, "legacy-photo fixture lost its photo_data"
    assert chunk.pre_parsed is None, "legacy entry must route to the full pipeline"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/goldens/test_readers_golden.py -v`
Expected: FAIL on every golden test with "No reader golden at ...". `test_the_legacy_photo_fixture_really_carries_a_photo` passes.

- [ ] **Step 3: Generate the expected files**

Run: `python -m pytest tests/goldens/test_readers_golden.py --update-goldens -q`
Expected: all passed; eight files written under `tests/goldens/readers/`.

- [ ] **Step 4: Read the generated goldens before trusting them**

Run:
```bash
python -c "
import json, pathlib
for p in sorted(pathlib.Path('tests/goldens/readers').glob('*.json')):
    d = json.loads(p.read_text(encoding='utf-8'))
    if 'chunks' in d:
        print(p.name, len(d['chunks']), 'chunks', 'imgs=', len(d['qualifying_images']))
        print('   first 120 chars:', repr(d['chunks'][0]['text'][:120]))
    else:
        print(p.name, sorted(d))
"
```
Expected: chunk counts match the Task 2 sanity check; the dual-units chunk text contains `[IMAGE: scones.jpg]`; the paprika golden shows `image_bytes_len` greater than 0. Fix the corpus and regenerate if anything looks wrong — do not commit a golden you have not read.

- [ ] **Step 5: Re-run in replay mode**

Run: `python -m pytest tests/goldens/test_readers_golden.py -q`
Expected: all passed with no `--update-goldens`.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -q`
Expected: no failures.

- [ ] **Step 7: Commit**

```bash
git add tests/goldens/test_readers_golden.py tests/goldens/readers
git commit -m "test(goldens): lock what each reader makes of a real file"
```

---

## Task 6: Prompt builders

A behaviour-preserving refactor: the inline f-strings in `gemini.py` and `toc.py` become pure functions so Task 7 can snapshot them. Nothing else changes — the rendered strings must be byte-identical.

**Files:**
- Modify: `recipeparser/gemini.py` (`normalise_baker_table`, `extract_recipe_from_text`, `extract_recipes`, `refine_recipe_for_cayenne`)
- Modify: `recipeparser/toc.py` (`_parse_toc_from_text_fallback`, `_classify_toc_recipe_indices`)
- Create: `tests/goldens/test_prompts_snapshot.py` (equivalence tests only; snapshots arrive in Task 7)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `recipeparser.gemini.build_extract_prompt(text: str, units: str = "book") -> str`
  - `recipeparser.gemini.build_plain_text_prompt(text: str) -> str`
  - `recipeparser.gemini.build_refine_prompt(raw: object, uom_system: str, measure_preference: str, axes: Dict[str, List[str]]) -> str`
  - `recipeparser.gemini.build_table_prompt(text: str) -> str`
  - `recipeparser.toc.build_toc_parse_prompt(chunks: List[str]) -> str`
  - `recipeparser.toc.build_toc_classify_prompt(titles: List[str]) -> str`

- [ ] **Step 1: Write the failing equivalence test**

Create `tests/goldens/test_prompts_snapshot.py`:

```python
"""Prompt and schema snapshots (spec §6.3).

The equivalence tests here are the refactor's safety net: each builder must
render exactly what the call site used to build inline.  The snapshots (added
in the next task) are what make prompt drift a reviewable diff.
"""
from __future__ import annotations

import pytest

from recipeparser import gemini, toc
from recipeparser.models import RecipeExtraction
from tests.goldens.conftest import FIXED_AXES

PLACEHOLDER_BODY = "PLACEHOLDER CHUNK BODY — fixed text so the snapshot only moves when the template does."

PLACEHOLDER_RECIPE = RecipeExtraction(
    name="Placeholder Cake",
    photo_filename="cake.jpg",
    servings="8",
    prep_time="20 mins",
    cook_time="35 mins",
    ingredients=["2 cups/250g plain flour", "1 cup sugar"],
    directions=["Mix everything.", "Bake until done."],
)


def _sent_contents(monkeypatch, call) -> str:
    """The prompt a call site actually hands to generate_content."""
    seen = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen["contents"] = contents
            raise RuntimeError("captured")

    class _Client:
        models = _Models()

    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)
    try:
        call(_Client())
    except Exception:
        pass
    return seen["contents"]


class TestBuildersMatchTheCallSites:
    @pytest.mark.parametrize("units", ["book", "metric", "us", "imperial"])
    def test_extract(self, monkeypatch, units):
        sent = _sent_contents(
            monkeypatch, lambda c: gemini.extract_recipes(PLACEHOLDER_BODY, c, units=units)
        )
        assert sent == gemini.build_extract_prompt(PLACEHOLDER_BODY, units)

    def test_plain_text(self, monkeypatch):
        sent = _sent_contents(
            monkeypatch, lambda c: gemini.extract_recipe_from_text(PLACEHOLDER_BODY, c)
        )
        assert sent == gemini.build_plain_text_prompt(PLACEHOLDER_BODY)

    def test_table(self, monkeypatch):
        sent = _sent_contents(
            monkeypatch, lambda c: gemini.normalise_baker_table(PLACEHOLDER_BODY, c)
        )
        assert sent == gemini.build_table_prompt(PLACEHOLDER_BODY)

    @pytest.mark.parametrize(
        "axes,measure",
        [({}, "Volume"), (FIXED_AXES, "Volume"), (FIXED_AXES, "Weight")],
    )
    def test_refine(self, monkeypatch, axes, measure):
        sent = _sent_contents(
            monkeypatch,
            lambda c: gemini.refine_recipe_for_cayenne(
                PLACEHOLDER_RECIPE, c, measure_preference=measure, user_axes=axes
            ),
        )
        assert sent == gemini.build_refine_prompt(
            PLACEHOLDER_RECIPE, "US", measure, axes
        )

    def test_toc_parse(self, monkeypatch):
        chunks = ["Contents", "Soups .... 3", "Puddings .... 41"]
        sent = _sent_contents(
            monkeypatch, lambda c: toc._parse_toc_from_text_fallback(chunks, c)
        )
        assert sent == toc.build_toc_parse_prompt(chunks)

    def test_toc_classify(self, monkeypatch):
        entries = [("Soups", 3), ("Boiled Custard", 41)]
        sent = _sent_contents(
            monkeypatch, lambda c: toc._classify_toc_recipe_indices(entries, c)
        )
        assert sent == toc.build_toc_classify_prompt([e[0] for e in entries])

    def test_toc_parse_truncates_a_long_body(self):
        rendered = toc.build_toc_parse_prompt(["x" * 30_000])
        assert "[... truncated ...]" in rendered
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/goldens/test_prompts_snapshot.py -v`
Expected: FAIL — `AttributeError: module 'recipeparser.gemini' has no attribute 'build_extract_prompt'`.

- [ ] **Step 3: Refactor `recipeparser/gemini.py`**

Add each builder immediately above the function that used to hold its f-string, and replace the inline string with a call. The bodies are the existing f-strings moved verbatim.

```python
def build_table_prompt(text_chunk: str) -> str:
    """The prompt that reformats a multi-column baker's percentage table."""
    return f"""The following text is from a recipe book and contains one or more ingredient tables
where each ingredient name, its weight, its volume measure, and its baker's percentage
appear on separate lines rather than in columns.

Reformat ONLY the ingredient table sections so that each ingredient appears on a single
line in the format: "IngredientName: weight (volume) — baker's%"

Do NOT change any recipe titles, headings, method steps, notes, or any other text.
Do NOT add or remove any ingredients or values.
Preserve all [IMAGE: ...] markers exactly as they appear.

Text:
{text_chunk}"""
```
In `normalise_baker_table`, replace the `prompt = f"""..."""` block with `prompt = build_table_prompt(text_chunk)`.

```python
def build_plain_text_prompt(text: str) -> str:
    """The prompt for a single recipe in plain text (Paprika import, pasted recipe)."""
    return f"""
You are a culinary data extractor. The following text is a recipe. Extract it.
...
"""
```
Copy the existing body verbatim, including its leading newline. In `extract_recipe_from_text`, replace the inline block with `prompt = build_plain_text_prompt(text)`.

```python
def build_extract_prompt(text_chunk: str, units: str = "book") -> str:
    """The prompt for a book chunk that may hold several recipes."""
    units_rule = _UNITS_RULES.get(units.lower(), "")
    units_section = f"\n{units_rule}" if units_rule else ""
    return f"""
You are a culinary data extractor. Review the following text from an EPUB recipe book.
...
"""
```
Move both the `units_rule` and `units_section` lines into the builder and copy the f-string verbatim. In `extract_recipes`, replace those two lines plus the inline block with `prompt = build_extract_prompt(text_chunk, units)`.

```python
def build_refine_prompt(
    raw_recipe: object,
    uom_system: str,
    measure_preference: str,
    user_axes: Optional[Dict[str, List[str]]] = None,
) -> str:
    """The Pass-2 prompt: structured ingredients, fat tokens, and categorisation."""
    categorization_section = _format_axes_for_prompt(user_axes or {})
    return f"""
You are a culinary data refiner. Transform this raw recipe into the structured Cayenne format.
...
"""
```
Copy the f-string verbatim, keeping the doubled braces around the fat-token example. In `refine_recipe_for_cayenne`, keep `axes = user_axes or {}` and `schema = _build_dynamic_grid_schema(axes)`, drop the local `categorization_section` assignment, and replace the inline block with:
```python
    prompt = build_refine_prompt(raw_recipe, uom_system, measure_preference, axes)
```

- [ ] **Step 4: Refactor `recipeparser/toc.py`**

```python
def build_toc_parse_prompt(chunks: List[str]) -> str:
    """The prompt that reads a contents page when the programmatic TOC is thin."""
    text = "\n\n".join(chunks)
    if len(text) > 20_000:
        text = text[:20_000] + "\n[... truncated ...]"
    prompt = """This text is from the table-of-contents or contents page of a recipe book.
...
Include only substantive entries (skip "Contents", "Index", etc. if they are standalone headers)."""
    return prompt + f"\n\nText:\n{text}"


def build_toc_classify_prompt(titles: List[str]) -> str:
    """The prompt that separates recipe titles from section headers."""
    prompt = """Given this list of table-of-contents entries from a cookbook, identify which ones are
...
specific dish/recipe names."""
    prompt += "\n\nEntries (one per line):\n"
    for index, title in enumerate(titles):
        prompt += f"{index}. {title}\n"
    return prompt
```
Copy both instruction blocks verbatim from the current call sites. In `_parse_toc_from_text_fallback`, keep the `if not chunks: return []` guard, then replace the text-joining, truncation and prompt lines with `prompt = build_toc_parse_prompt(chunks)`. In `_classify_toc_recipe_indices`, keep the `if not entries: return None` guard and `titles = [e[0] for e in entries]`, then replace the prompt lines with `prompt = build_toc_classify_prompt(titles)`.

- [ ] **Step 5: Run the equivalence tests**

Run: `python -m pytest tests/goldens/test_prompts_snapshot.py -v`
Expected: all passed.

- [ ] **Step 6: Prove the refactor changed no behaviour**

Run: `python -m pytest tests/test_gemini.py tests/test_toc.py tests/snapshots -v`
Expected: all passed, with the same counts as before the refactor. These suites already assert on prompt contents, so a drifted string fails here.

- [ ] **Step 7: Run the whole suite and the linters**

Run: `python -m pytest -q`
Expected: no failures.

Run: `python -m ruff check recipeparser tests` then `python -m mypy recipeparser`
Expected: no new findings versus `baseline_ruff.txt` / `baseline_mypy.txt`.

- [ ] **Step 8: Commit**

```bash
git add recipeparser/gemini.py recipeparser/toc.py tests/goldens/test_prompts_snapshot.py
git commit -m "refactor(gemini,toc): make every prompt a function you can read"
```

---

## Task 7: Prompt and schema snapshots

Turns the builders into reviewable diffs, and locks `_schema_for_gemini` so Gemini's `additionalProperties` rejection cannot creep back.

**Files:**
- Modify: `tests/goldens/test_prompts_snapshot.py`
- Create: `tests/goldens/__snapshots__/test_prompts_snapshot.ambr`

**Interfaces:**
- Consumes: the six builders from Task 6, `FIXED_AXES`.
- Produces: nothing later tasks import.

- [ ] **Step 1: Write the failing snapshot tests**

Append to `tests/goldens/test_prompts_snapshot.py`:

```python
from syrupy.assertion import SnapshotAssertion

from recipeparser.models import CayenneRefinement, RecipeList, TocList, TocRecipeClassification


class TestPromptSnapshots:
    @pytest.mark.parametrize("units", ["book", "metric", "us", "imperial"])
    def test_extract_prompt(self, snapshot: SnapshotAssertion, units):
        assert gemini.build_extract_prompt(PLACEHOLDER_BODY, units) == snapshot(name=f"extract-{units}")

    def test_plain_text_prompt(self, snapshot: SnapshotAssertion):
        assert gemini.build_plain_text_prompt(PLACEHOLDER_BODY) == snapshot

    def test_table_prompt(self, snapshot: SnapshotAssertion):
        assert gemini.build_table_prompt(PLACEHOLDER_BODY) == snapshot

    def test_refine_prompt_without_axes(self, snapshot: SnapshotAssertion):
        assert gemini.build_refine_prompt(PLACEHOLDER_RECIPE, "US", "Volume", {}) == snapshot

    def test_refine_prompt_with_axes(self, snapshot: SnapshotAssertion):
        assert gemini.build_refine_prompt(PLACEHOLDER_RECIPE, "US", "Volume", FIXED_AXES) == snapshot

    def test_refine_prompt_weight_preference(self, snapshot: SnapshotAssertion):
        assert gemini.build_refine_prompt(PLACEHOLDER_RECIPE, "Metric", "Weight", FIXED_AXES) == snapshot

    def test_toc_parse_prompt(self, snapshot: SnapshotAssertion):
        assert toc.build_toc_parse_prompt(["Contents", "Soups .... 3"]) == snapshot

    def test_toc_classify_prompt(self, snapshot: SnapshotAssertion):
        assert toc.build_toc_classify_prompt(["Soups", "Boiled Custard"]) == snapshot


class TestSchemaSnapshots:
    """The only guard that additionalProperties cannot creep back into a schema."""

    def test_recipe_list_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(RecipeList) == snapshot

    def test_cayenne_refinement_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(CayenneRefinement) == snapshot

    def test_dynamic_grid_schema(self, snapshot: SnapshotAssertion):
        model = gemini._build_dynamic_grid_schema(FIXED_AXES)
        assert gemini._schema_for_gemini(model) == snapshot

    def test_toc_list_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(TocList) == snapshot

    def test_toc_classification_schema(self, snapshot: SnapshotAssertion):
        assert gemini._schema_for_gemini(TocRecipeClassification) == snapshot

    @pytest.mark.parametrize(
        "model", [RecipeList, CayenneRefinement, TocList, TocRecipeClassification]
    )
    def test_no_schema_carries_additional_properties(self, model):
        rendered = json.dumps(gemini._schema_for_gemini(model))
        assert "additionalProperties" not in rendered

    def test_the_dynamic_grid_schema_carries_no_additional_properties(self):
        model = gemini._build_dynamic_grid_schema(FIXED_AXES)
        assert "additionalProperties" not in json.dumps(gemini._schema_for_gemini(model))
```

Add `import json` to the module's imports.

- [ ] **Step 2: Run to verify the snapshot tests fail**

Run: `python -m pytest tests/goldens/test_prompts_snapshot.py -v`
Expected: the snapshot tests FAIL with "snapshot does not exist"; the `additionalProperties` assertions and the Task 6 equivalence tests pass.

If `TocRecipeClassification` is not importable from `recipeparser.models`, run `python -c "import recipeparser.models as m; print([n for n in dir(m) if 'Toc' in n])"` and use the names it prints.

- [ ] **Step 3: Generate the snapshots**

Run: `python -m pytest tests/goldens/test_prompts_snapshot.py --snapshot-update -q`
Expected: all passed; `tests/goldens/__snapshots__/test_prompts_snapshot.ambr` written.

- [ ] **Step 4: Read the snapshot file before trusting it**

Run: `python -m pytest tests/goldens/test_prompts_snapshot.py -q` then open `tests/goldens/__snapshots__/test_prompts_snapshot.ambr`.
Expected: the four `extract-*` entries differ only in the units rule; the three refine entries differ only in the categorisation section and the CONTEXT lines; every schema entry is free of `additionalProperties`.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: no failures.

- [ ] **Step 6: Commit**

```bash
git add tests/goldens/test_prompts_snapshot.py tests/goldens/__snapshots__
git commit -m "test(goldens): make prompt and schema drift a diff you can review"
```

---

## Task 8: Stage replay goldens

The first family to run real Gemini output through the real parse path: `json.loads`, `model_validate`, string-to-list coercion, the dynamic grid schema round-trip, clean-grid tag stripping, fat-token validation, table normalisation, and vision OCR.

**This task contains the first `--record-gemini` run.** It needs a real `GOOGLE_API_KEY` and costs money. It cannot be done by an agent without that key — stop and hand Step 3 to the user if you do not have one.

**Files:**
- Create: `tests/goldens/test_stages_golden.py`
- Create: `tests/goldens/gemini/<fixture>/...` (recorded)
- Create: `tests/goldens/__snapshots__/test_stages_golden.ambr`

**Interfaces:**
- Consumes: the `golden_client` factory, `FIXED_AXES`, the reader goldens' corpus paths.
- Produces: recordings under `gemini/` that Task 9 also replays for `dual-units.epub`, `phases-bakers.epub` and `legacy-photo.paprikarecipes`.

- [ ] **Step 1: Write the test**

Create `tests/goldens/test_stages_golden.py`:

```python
"""Stage replay goldens (spec §6.2).

Real recorded Gemini replies through the real parse path, offline.  Re-record
with `pytest tests/goldens --record-gemini` and commit the prompt, the
recordings and the resulting snapshot changes together.
"""
from __future__ import annotations

import pytest
from syrupy.assertion import SnapshotAssertion

from recipeparser import gemini
from recipeparser.core.models import InputType
from recipeparser.core.stages.categorize import categorize
from recipeparser.core.stages.extract import extract
from recipeparser.core.stages.refine import refine
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.readers.pdf import PdfReader, extract_text_from_pdf
from recipeparser.io.readers.url import UrlReader
from tests.goldens.conftest import FIXED_AXES
from tests.goldens.paths import corpus_path

#: Every fixture that reaches the extract/refine/categorize path, and how its
#: chunks are produced.  scanned.pdf is absent: it never gets past preflight,
#: so its Gemini coverage is the vision test below.
STAGE_FIXTURES = (
    "gutenberg-multi.epub",
    "dual-units.epub",
    "phases-bakers.epub",
    "text-pages.pdf",
    "legacy-photo.paprikarecipes",
)


def _chunks_for(fixture: str, monkeypatch):
    if fixture.endswith(".epub"):
        return EpubReader().read(str(corpus_path(fixture)))
    if fixture.endswith(".pdf"):
        return PdfReader().read(str(corpus_path(fixture)))
    if fixture.endswith(".paprikarecipes"):
        return PaprikaReader().read(str(corpus_path(fixture)))
    raise AssertionError(f"no reader for {fixture}")


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    """A recorded parse retry must not cost a real second of wall clock."""
    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)


@pytest.mark.parametrize("fixture", STAGE_FIXTURES)
def test_stage_golden(fixture, golden_client, snapshot: SnapshotAssertion, monkeypatch):
    client = golden_client(fixture)
    chunks = _chunks_for(fixture, monkeypatch)

    rendered = []
    for chunk in chunks:
        text = chunk.text
        if gemini.needs_table_normalisation(text):
            text = gemini.normalise_baker_table(text, client)
        extractions = extract(
            chunk_text=text,
            client=client,
            units="book",
            plain_text_mode=chunk.input_type == InputType.PAPRIKA_LEGACY,
        )
        for raw in extractions:
            refined = refine(
                raw=raw,
                client=client,
                uom_system="US",
                measure_preference="Volume",
                user_axes=FIXED_AXES,
            )
            rendered.append(
                {
                    "raw": raw.model_dump(),
                    "refined": refined.model_dump(),
                    "categories": categorize(recipe=refined, user_axes=FIXED_AXES),
                }
            )

    assert rendered == snapshot


def test_vision_ocr_golden(golden_client, snapshot: SnapshotAssertion):
    """The scanned fixture's only path through Gemini."""
    client = golden_client("scanned.pdf")
    text = extract_text_from_pdf(str(corpus_path("scanned.pdf")), client)
    assert text == snapshot


def test_the_bakers_table_is_normalised_before_extraction(golden_client):
    """phases-bakers is in the corpus for this branch; assert it actually fires."""
    client = golden_client("phases-bakers.epub")
    chunks = EpubReader().read(str(corpus_path("phases-bakers.epub")))
    triggering = [c for c in chunks if gemini.needs_table_normalisation(c.text)]
    assert triggering, "phases-bakers.epub no longer triggers needs_table_normalisation"
    normalised = gemini.normalise_baker_table(triggering[0].text, client)
    assert normalised != triggering[0].text


def test_every_refined_recipe_keeps_its_grid_inside_the_axes(golden_client, monkeypatch):
    """Clean-grid stripping: a tag outside FIXED_AXES must never survive."""
    valid = {axis: set(tags) for axis, tags in FIXED_AXES.items()}
    client = golden_client("dual-units.epub")
    for chunk in EpubReader().read(str(corpus_path("dual-units.epub"))):
        for raw in extract(chunk_text=chunk.text, client=client, units="book"):
            refined = refine(
                raw=raw, client=client, uom_system="US",
                measure_preference="Volume", user_axes=FIXED_AXES,
            )
            for axis, tags in refined.grid_categories.items():
                assert axis in valid
                assert set(tags) <= valid[axis]
```

- [ ] **Step 2: Run it to confirm it fails for the right reason**

Run: `python -m pytest tests/goldens/test_stages_golden.py -v`
Expected: FAIL with `MissingRecordingError: No recorded Gemini reply at tests/goldens/gemini/...` naming a real path. That message is the contract from spec §8 — confirm it names the file it wanted.

- [ ] **Step 3: Record the replies (needs a real API key; costs money)**

Export a real key in the shell, then run:
```bash
python -m pytest tests/goldens/test_stages_golden.py --record-gemini -q
```
Expected: the run makes real calls and writes files under `tests/goldens/gemini/<fixture>/<sha8>/`. Snapshot assertions still fail on this run — that is expected; Step 5 generates them.

If the run fails with `--record-gemini needs a real GOOGLE_API_KEY`, the shell still has the test sentinel; export the real key and retry.

- [ ] **Step 4: Inspect what was recorded**

Run:
```bash
python -c "
import json, pathlib
root = pathlib.Path('tests/goldens/gemini')
for p in sorted(root.rglob('*.json')):
    d = json.loads(p.read_text(encoding='utf-8'))
    print(p.relative_to(root), d['stage'], d['ordinal'], len(d['response_text']), 'chars')
print('total', sum(1 for _ in root.rglob('*.json')), 'recordings')
"
```
Expected: one directory per distinct prompt body; `extract-00`/`refine-00` per body; `table-00` under the phases-bakers table body; `vision-00`/`vision-01` under the scanned fixture. No file has an empty `response_text` unless a real reply was empty.

- [ ] **Step 5: Generate the snapshots offline**

Run: `python -m pytest tests/goldens/test_stages_golden.py --snapshot-update -q`
Expected: all passed, with no network (no `--record-gemini`).

- [ ] **Step 6: Read the snapshot before trusting it**

Open `tests/goldens/__snapshots__/test_stages_golden.ambr`.
Expected: `dual-units` recipes have `photo_filename` set from the `[IMAGE: ...]` markers; `phases-bakers` keeps `**Phase 1**` / `**Phase 2**` entries as their own list items; every `refined` block has fat tokens of the form `{{ing_NN|...}}`; `legacy-photo` produced one recipe.

If a recipe's `grid_categories` is empty across the board, confirm `FIXED_AXES` reached the prompt — a fixed-axes mismatch is a recording bug, not a model quirk.

- [ ] **Step 7: Confirm replay is deterministic and offline**

Run: `python -m pytest tests/goldens/test_stages_golden.py -q` twice.
Expected: passed both times, identical output.

- [ ] **Step 8: Run the whole suite**

Run: `python -m pytest -q`
Expected: no failures.

- [ ] **Step 9: Commit prompt, recordings and output together**

```bash
git add tests/goldens/test_stages_golden.py tests/goldens/gemini tests/goldens/__snapshots__
git commit -m "test(goldens): run real replies through the real parse path"
```

---

## Task 9: End-to-end goldens

Runs three fixtures through `RecipePipeline` at the production pool size and out through both zip writers, comparing order-independently. This is the only coverage the `ThreadPoolExecutor` path, the per-chunk error boundary and the progress callback get without every stage patched.

**Files:**
- Create: `tests/goldens/test_e2e_golden.py`
- Create: `tests/goldens/e2e/<fixture>/{ingest,paprika,cayenne}.json`
- Modify: `tests/goldens/corpus/README.md` (no change needed if Task 2's regeneration section is already written; confirm it is)

**Interfaces:**
- Consumes: the `golden_client` factory, `FIXED_AXES`, `update_goldens`, and the recordings from Task 8.
- Produces: nothing later tasks import.

- [ ] **Step 1: Write the test**

Create `tests/goldens/test_e2e_golden.py`:

```python
"""End-to-end goldens (spec §6.4).

Reader → RecipePipeline at the production pool size → both zip writers, with
recorded Gemini replies.  Comparison is a multiset sorted by title, so a
regression that made output depend on completion order fails here.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from recipeparser.core.fsm import PipelineController
from recipeparser.core.pipeline import MAX_CONCURRENT_API_CALLS, RecipePipeline
from recipeparser.core.ports import CategorySource
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.writers.cayenne_zip import CayenneZipWriter
from recipeparser.io.writers.paprika_zip import PaprikaWriter
from recipeparser import gemini
from tests.goldens.conftest import FIXED_AXES
from tests.goldens.paths import E2E_DIR, corpus_path

E2E_FIXTURES = ("dual-units.epub", "phases-bakers.epub", "legacy-photo.paprikarecipes")

PLACEHOLDER = "<fixed>"


class _FixedAxesSource(CategorySource):
    """The taxonomy the recordings were made against."""

    def load_axes(self, user_id: str = "") -> Dict[str, List[str]]:
        return FIXED_AXES

    def load_category_ids(self, user_id: str = "") -> Dict[str, str]:
        return {}


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)


def _read(fixture: str):
    if fixture.endswith(".epub"):
        return EpubReader().read(str(corpus_path(fixture)))
    return PaprikaReader().read(str(corpus_path(fixture)))


def _normalise(value):
    """Replace everything that legitimately differs per run.

    uid, hash and created are generated at write time; photo_data and the
    embedding are large and are compared by digest.
    """
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if key in ("uid", "hash", "created"):
                out[key] = PLACEHOLDER
            elif key == "photo_data" and inner:
                out[key] = "sha256:" + hashlib.sha256(str(inner).encode("utf-8")).hexdigest()
            elif key == "embedding" and inner:
                digest = hashlib.sha256(json.dumps(inner).encode("utf-8")).hexdigest()
                out[key] = {"len": len(inner), "sha256": digest}
            else:
                out[key] = _normalise(inner)
        return out
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _zip_manifest(path: Path) -> dict:
    """Entry names plus the parsed JSON inside each entry, sorted by name."""
    entries = {}
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            raw = zf.read(name)
            try:
                payload = json.loads(gzip.decompress(raw))
            except gzip.BadGzipFile:
                payload = json.loads(raw)
            entries[name] = _normalise(payload)
    return {"names": sorted(entries), "entries": entries}


def _assert_golden(fixture: str, name: str, actual: dict, update: bool) -> None:
    path = E2E_DIR / fixture / f"{name}.json"
    rendered = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), (
        f"No e2e golden at {path}. Create it with:\n"
        "    pytest tests/goldens/test_e2e_golden.py --update-goldens"
    )
    assert json.loads(rendered) == json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", E2E_FIXTURES)
def test_e2e_golden(fixture, golden_client, tmp_path, update_goldens):
    GlobalRateLimiter().reset()
    progress: List[tuple] = []
    pipeline = RecipePipeline(
        client=golden_client(fixture),
        controller=PipelineController(),
        category_source=_FixedAxesSource(),
        uom_system="US",
        measure_preference="Volume",
        concurrency=MAX_CONCURRENT_API_CALLS,
        rpm=9999,
    )
    results = pipeline.run(
        _read(fixture),
        on_progress=lambda stage, done, total: progress.append((stage, done, total)),
    )
    assert results, f"{fixture} produced no recipes"
    assert progress, "on_progress never fired"
    assert progress[-1][1] == progress[-1][2], "progress did not reach completion"

    ordered = sorted(results, key=lambda r: r.title)
    _assert_golden(
        fixture,
        "ingest",
        {"recipes": [_normalise(r.model_dump()) for r in ordered]},
        update_goldens,
    )

    paprika_path = tmp_path / "out.paprikarecipes"
    PaprikaWriter(paprika_path).write(ordered)
    _assert_golden(fixture, "paprika", _zip_manifest(paprika_path), update_goldens)

    cayenne_path = tmp_path / "out.cayenne.paprikarecipes"
    CayenneZipWriter(cayenne_path).write(ordered)
    _assert_golden(fixture, "cayenne", _zip_manifest(cayenne_path), update_goldens)


@pytest.mark.parametrize("fixture", E2E_FIXTURES)
def test_the_result_is_the_same_at_pool_size_one(fixture, golden_client, update_goldens):
    """Order independence, asserted rather than assumed."""
    if update_goldens:
        pytest.skip("nothing to compare while regenerating")

    def _run(concurrency: int):
        GlobalRateLimiter().reset()
        pipeline = RecipePipeline(
            client=golden_client(fixture),
            controller=PipelineController(),
            category_source=_FixedAxesSource(),
            concurrency=concurrency,
            rpm=9999,
        )
        results = pipeline.run(_read(fixture))
        return [_normalise(r.model_dump()) for r in sorted(results, key=lambda r: r.title)]

    assert _run(1) == _run(MAX_CONCURRENT_API_CALLS)


def test_the_cayenne_archive_round_trips_back_through_the_reader(golden_client, tmp_path):
    """Flow B: what CayenneZipWriter writes, PaprikaReader must restore for free."""
    GlobalRateLimiter().reset()
    pipeline = RecipePipeline(
        client=golden_client("dual-units.epub"),
        controller=PipelineController(),
        category_source=_FixedAxesSource(),
        concurrency=MAX_CONCURRENT_API_CALLS,
        rpm=9999,
    )
    results = pipeline.run(_read("dual-units.epub"))
    out = tmp_path / "roundtrip.paprikarecipes"
    CayenneZipWriter(out).write(results)

    restored = PaprikaReader().read(str(out))
    assert len(restored) == len(results)
    assert all(c.pre_parsed is not None for c in restored)
    assert sorted(c.pre_parsed.title for c in restored) == sorted(r.title for r in results)
```

- [ ] **Step 2: Run it to see what is missing**

Run: `python -m pytest tests/goldens/test_e2e_golden.py -v`
Expected: either `MissingRecordingError` (the pipeline sends prompts Task 8 did not record — different `units` key: the pipeline maps `uom_system="US"` to `units="us"`, while Task 8 recorded `units="book"`) or a missing-golden failure.

The `units` difference changes the prompt but **not** the body, so the recordings key identically and replay works. Confirm which failure you got before continuing; if it is `MissingRecordingError`, the missing path names the fixture and stage — record it in Step 3.

- [ ] **Step 3: Record any calls the pipeline makes that Task 8 did not**

Only if Step 2 reported `MissingRecordingError`. With a real key exported:
```bash
python -m pytest tests/goldens/test_e2e_golden.py --record-gemini --update-goldens -q
```
Expected: new recordings appear under `tests/goldens/gemini/`; goldens are written.

Otherwise skip to Step 4.

- [ ] **Step 4: Generate the expected files offline**

Run: `python -m pytest tests/goldens/test_e2e_golden.py --update-goldens -q`
Expected: all passed; nine files under `tests/goldens/e2e/`.

- [ ] **Step 5: Read the goldens before trusting them**

Run:
```bash
python -c "
import json, pathlib
for p in sorted(pathlib.Path('tests/goldens/e2e').rglob('*.json')):
    d = json.loads(p.read_text(encoding='utf-8'))
    if 'recipes' in d:
        print(p, [r['title'] for r in d['recipes']])
    else:
        print(p, d['names'])
"
```
Expected: `dual-units` yields "Buttermilk Scones" and "Brown Butter Shortbread"; `phases-bakers` yields the two bread recipes; `legacy-photo` yields "Boiled Custard". Zip entry names match those titles. Every `uid`, `hash` and `created` reads `<fixed>`; every `embedding` is a `{len, sha256}` object with `len` 1536.

- [ ] **Step 6: Run in comparison mode**

Run: `python -m pytest tests/goldens/test_e2e_golden.py -q`
Expected: all passed with no flags.

- [ ] **Step 7: Confirm it is genuinely offline**

Run: `python -m pytest tests/goldens -q -W error::UserWarning`
Expected: passed. A `prompt_sha256` mismatch warning becomes an error here, so this also proves no prompt has drifted since recording. If it fires, the prompt changed after the recording — re-record rather than suppressing it.

- [ ] **Step 8: Run the whole suite**

Run: `python -m pytest -q`
Expected: no failures.

- [ ] **Step 9: Commit**

```bash
git add tests/goldens/test_e2e_golden.py tests/goldens/e2e tests/goldens/gemini
git commit -m "test(goldens): a book in, two archives out, order-independent"
```

---

## Task 10: Close the loop

Documentation the next person needs, and proof the whole thing runs the way CI will run it.

**Files:**
- Modify: `tests/goldens/corpus/README.md` (add the "What each family covers" section)
- Modify: `CHANGELOG.md`
- Modify: `tests/goldens/test_golden_harness.py` (add the recordings-are-reachable guard)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Write the guard test**

Append to `tests/goldens/test_golden_harness.py`:

```python
def test_every_recording_is_valid_and_named_by_its_own_key():
    """A recording whose filename disagrees with its contents would replay wrongly."""
    import json

    from tests.goldens import golden_client as gc

    seen = 0
    for path in paths.GEMINI_DIR.rglob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert set(payload) >= {
            "stage", "ordinal", "model", "config", "prompt_sha256", "response_text"
        }, f"{path} is missing fields"
        assert "response_json_schema" not in payload["config"], f"{path} stored a schema"
        assert path.name == f"{payload['stage']}-{payload['ordinal']:02d}.json", path
        assert len(path.parent.name) == 8, f"{path.parent} is not a body sha8 directory"
        assert path.parent.parent.name in paths.CORPUS_FIXTURES, path
        seen += 1
    assert seen > 0, "no recordings found — tests/goldens/gemini/ is empty"


def test_every_fixture_that_calls_gemini_has_recordings():
    expected = {
        "gutenberg-multi.epub", "dual-units.epub", "phases-bakers.epub",
        "text-pages.pdf", "scanned.pdf", "legacy-photo.paprikarecipes",
    }
    present = {d.name for d in paths.GEMINI_DIR.iterdir() if d.is_dir()}
    assert expected <= present, f"no recordings for {sorted(expected - present)}"
```

- [ ] **Step 2: Run it to verify it passes**

Run: `python -m pytest tests/goldens/test_golden_harness.py -v`
Expected: all passed. A failure here means a recording is misnamed — fix the file, do not relax the assertion.

- [ ] **Step 3: Add the coverage section to `corpus/README.md`**

Append:

```markdown
## What each family covers

| File | What it locks |
|---|---|
| `test_readers_golden.py` | Every reader's chunks and qualifying images against a real file; the scanned PDF's preflight refusal; `utils.html_to_text`. No Gemini. |
| `test_prompts_snapshot.py` | The six prompt builders across the units, axes and measure-preference matrix, and `_schema_for_gemini` for every response model. This is the only guard that `additionalProperties` cannot creep back. |
| `test_stages_golden.py` | Real recorded replies through `json.loads`, `model_validate`, the dynamic grid round-trip, clean-grid tag stripping and fat-token validation; the baker's-table branch; the vision OCR fallback. |
| `test_e2e_golden.py` | `RecipePipeline` at pool size 4 through both zip writers, compared as an order-independent multiset; the Cayenne archive's round trip back through `PaprikaReader`. |
| `test_golden_client.py` | The keying rules — which recording a call belongs to, at any pool size. |
| `test_golden_harness.py` | The corpus/README cross-check, the 2 MB budget, and that every recording is named by its own key. |

## Not covered here

A live quality eval (running the corpus against real Gemini and reporting
`run_recon` results with tolerance), broader OCR than the one scanned fixture,
migrating the older mock-based reader and TOC tests onto this corpus, and a
Supabase writer cassette. TOC calls are recorded by stage but not replayed:
`toc.py` reads `response.parsed`, which `GoldenResponse` leaves as None.
```

- [ ] **Step 4: Add a CHANGELOG entry**

Add under the unreleased heading, matching the file's existing style:

```markdown
### Added
- Golden test suite (`tests/goldens/`): a seven-file real-input corpus, recorded
  Gemini replies replayed offline, and goldens for readers, prompts, schemas,
  stage parsing, and the full pipeline through both zip writers.

### Changed
- `gemini.py` and `toc.py` build their prompts through named functions
  (`build_extract_prompt`, `build_refine_prompt`, `build_table_prompt`,
  `build_plain_text_prompt`, `build_toc_parse_prompt`,
  `build_toc_classify_prompt`). Behaviour is unchanged; the prompts are now
  snapshot-tested.
```

- [ ] **Step 5: Run the suite exactly as CI runs it**

Run:
```bash
DISABLE_AUTH=1 TEST_USER_ID=00000000-0000-0000-0000-000000000001 \
SUPABASE_JWT_SECRET=ci-dummy-secret-not-used-in-production \
python -m pytest tests/ -v --ignore=tests/smoke_test_exe.py --ignore=tests/smoke_test_docker.py --tb=short
```
Expected: all passed. This is the command at `.github/workflows/build-installer.yml:68`, with its env block.

- [ ] **Step 6: Prove the suite needs no network**

Run:
```bash
python -m pytest tests/goldens -q -p no:randomly 2>&1 | tail -5
```
with the machine's network disabled, or with `GOOGLE_API_KEY` unset.
Expected: all passed. Any failure naming a socket, DNS or `google.genai` is a real leak — find the unpatched call rather than skipping the test.

- [ ] **Step 7: Run the linters**

Run: `python -m ruff check recipeparser tests tools` then `python -m mypy recipeparser`
Expected: no new findings versus `baseline_ruff.txt` / `baseline_mypy.txt`.

- [ ] **Step 8: Commit**

```bash
git add tests/goldens/corpus/README.md tests/goldens/test_golden_harness.py CHANGELOG.md
git commit -m "docs(goldens): say what each family locks and how to re-record"
```

- [ ] **Step 9: Record the final numbers**

Run: `python -m pytest -q 2>&1 | tail -3`
Expected: a passing line. Write the final count into the PR description alongside the 578 baseline, and note that Tasks 8 and 9 made real Gemini calls whose replies are committed under `tests/goldens/gemini/`.

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| §3 Layout | 1 (package, paths, conftest), 2 (corpus), 5–9 (test files) |
| §4 Corpus, seven files, README, 2 MB | 2 |
| §5.1 Replay/record modes, key guard | 4 |
| §5.2 Keys: body sha8, stage sniff, per-body ordinal | 3 |
| §5.3 File shape, schema omitted from config | 4, guarded again in 10 |
| §5.4 `.text`, missing file fails loudly, hash mismatch warns | 4 |
| §5.5 Seeded 1536-float embeddings, never recorded | 4 |
| §5.6 Concurrency, lock on write only | 4 (lock), 9 (pool size 4, pool-size-1 equivalence) |
| §6.1 Reader goldens, `--update-goldens`, URL patch, scanned preflight | 5 |
| §6.2 Stage replay, table branch, vision branch | 8 |
| §6.3 Prompt-builder refactor, prompt matrix, schema snapshots | 6, 7 |
| §6.4 E2E at pool size 4, multiset compare, zip manifests, normalisation | 9 |
| §7 CI, re-recording, updating, determinism, existing tests untouched | 10 |
| §8 Error handling table | 4 (missing file, key guard, unknown prompt, hash warning, unparseable reply), 2 (README cross-check) |
| §9 Deferred | 10 (documented as not covered) |

**Deviations from the spec, all deliberate and stated at their task**

1. `pytest_addoption` lives in `tests/conftest.py`, not `tests/goldens/conftest.py` — verified 2026-09-07 that pytest silently ignores it in a nested conftest (Task 1).
2. Record mode rejects the literal `dummy-key-for-tests`, not merely an empty key, because `tests/conftest.py` always sets it (Task 4).
3. The stage sniff reads the first 600 characters rather than the first line: the table prompt's marker (`baker's percentage`) sits on its second line (Task 3).
4. `toc-classify` bodies exclude the instruction header, matching every other stage. Both TOC stages are recorded but not replayed — `toc.py` reads `response.parsed`, which `GoldenResponse` leaves as None (Tasks 3, 4, documented in 10).
5. The reader golden's chunk shape carries more than the spec's three fields: raw `image_bytes` cannot go in JSON and is stored as SHA-256 plus length, which is exactly what the `legacy-photo` fixture exists to cover (Task 5).
6. The scanned PDF's golden stores a message prefix, not the whole message, because the preflight text embeds an absolute path (Task 5).
7. `gutenberg-multi.epub`'s nav and NCX are regenerated by ebooklib during the trim. They are a real two-file TOC, not a hand-written stub, and the README says so (Task 2).
