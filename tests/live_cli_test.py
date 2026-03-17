#!/usr/bin/env python3
"""
RecipeParser Live CLI Tests
Run: python tests/live_cli_test.py
from RecipeParser/RecipeParser after pip install -e .
Requires GOOGLE_API_KEY in environment or .env file.
"""
from __future__ import annotations
import gzip, json, os, shutil, subprocess, sys, tempfile, time, zipfile
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

try:
    from rich.console import Console
    from rich.table import Table
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
FIXTURES_DIR = (
    REPO_ROOT.parent.parent
    / "cayenne-app" / "src" / "services" / "__tests__" / "fixtures"
)


@dataclass
class TestResult:
    name: str
    passed: bool = False
    elapsed: float = 0.0
    detail: str = ""

    def fail(self, msg: str) -> "TestResult":
        self.passed = False
        self.detail = msg
        return self

    def ok(self, msg: str = "") -> "TestResult":
        self.passed = True
        self.detail = msg
        return self


def _run(fn) -> TestResult:
    r = TestResult(name=fn.__name__)
    t0 = time.time()
    try:
        fn(r)
    except Exception as exc:
        r.fail(f"{type(exc).__name__}: {exc}")
    r.elapsed = time.time() - t0
    return r


def _run_cli(*args, **kwargs):
    """Run python -m recipeparser with given args. Returns CompletedProcess."""
    cmd = [sys.executable, "-m", "recipeparser"] + list(args)
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), **kwargs
    )


def _read_paprikarecipes(path: str) -> list:
    """Open a .paprikarecipes ZIP and return list of parsed recipe dicts."""
    recipes = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            raw = zf.read(name)
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
            recipes.append(data)
    return recipes


def _make_epub(title: str = "Test Pancakes") -> tuple:
    """Generate a minimal valid EPUB using ebooklib. Returns (bytes, tmp_path)."""
    from ebooklib import epub as E
    book = E.EpubBook()
    book.set_identifier("test-cli-001")
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
    import tempfile as _tmp
    with _tmp.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp_path = tmp.name
    E.write_epub(tmp_path, book)
    return Path(tmp_path).read_bytes(), tmp_path


def _make_pdf(title: str = "Test Beef Stew") -> tuple:
    """Generate a minimal PDF using PyMuPDF. Returns (bytes, tmp_path)."""
    import fitz
    import tempfile as _tmp
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
    with _tmp.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
    doc.save(tmp_path)
    doc.close()
    return Path(tmp_path).read_bytes(), tmp_path


# ── Test stubs ───────────────────────────────────────────────────────────────

def test_version(r: TestResult) -> None:
    """--version exits 0 and prints a version string."""
    proc = _run_cli("--version")
    if proc.returncode != 0:
        r.fail(f"exit {proc.returncode}; stderr={proc.stderr.strip()[:120]}")
        return
    output = (proc.stdout + proc.stderr).strip()
    if not output:
        r.fail("no output from --version")
        return
    r.ok(f"version={output[:60]}")


def test_error_no_args(r: TestResult) -> None:
    """No arguments → non-zero exit."""
    proc = _run_cli()
    if proc.returncode == 0:
        r.fail("expected non-zero exit with no args, got 0")
        return
    r.ok(f"exit={proc.returncode}")


def test_error_bad_extension(r: TestResult) -> None:
    """Passing a .txt file → non-zero exit (unsupported format)."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp.write(b"not a recipe book")
        tmp_path = tmp.name
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            proc = _run_cli(tmp_path, "--output", out_dir)
        if proc.returncode == 0:
            r.fail("expected non-zero exit for .txt file, got 0")
            return
        r.ok(f"exit={proc.returncode}")
    finally:
        os.unlink(tmp_path)


def test_error_nonexistent_file(r: TestResult) -> None:
    """Passing a path that doesn't exist → non-zero exit."""
    with tempfile.TemporaryDirectory() as out_dir:
        proc = _run_cli("/nonexistent/path/recipe.epub", "--output", out_dir)
    if proc.returncode == 0:
        r.fail("expected non-zero exit for missing file, got 0")
        return
    r.ok(f"exit={proc.returncode}")


def test_epub_ingest(r: TestResult) -> None:
    """EPUB ingest → exit 0, .paprikarecipes created, ≥1 recipe with valid fields."""
    _, epub_path = _make_epub("Test Pancakes")
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            proc = _run_cli(epub_path, "--output", out_dir)
            if proc.returncode != 0:
                r.fail(f"exit {proc.returncode}; stderr={proc.stderr.strip()[:200]}")
                return
            archives = list(Path(out_dir).glob("*.paprikarecipes"))
            if not archives:
                r.fail(f"no .paprikarecipes file in output dir; stdout={proc.stdout[:120]}")
                return
            recipes = _read_paprikarecipes(str(archives[0]))
            if not recipes:
                r.fail("archive is empty (0 recipes)")
                return
            rec = recipes[0]
            missing = [f for f in ("name", "ingredients", "directions") if not rec.get(f)]
            if missing:
                r.fail(f"recipe missing fields: {missing}")
                return
            r.ok(f"{len(recipes)} recipe(s); first='{rec['name'][:40]}'")
    finally:
        os.unlink(epub_path)


def test_pdf_ingest(r: TestResult) -> None:
    """PDF ingest → exit 0, .paprikarecipes created, ≥1 recipe with valid fields."""
    _, pdf_path = _make_pdf("Test Beef Stew")
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            proc = _run_cli(pdf_path, "--output", out_dir)
            if proc.returncode != 0:
                r.fail(f"exit {proc.returncode}; stderr={proc.stderr.strip()[:200]}")
                return
            archives = list(Path(out_dir).glob("*.paprikarecipes"))
            if not archives:
                r.fail(f"no .paprikarecipes file in output dir; stdout={proc.stdout[:120]}")
                return
            recipes = _read_paprikarecipes(str(archives[0]))
            if not recipes:
                r.fail("archive is empty (0 recipes)")
                return
            rec = recipes[0]
            missing = [f for f in ("name", "ingredients", "directions") if not rec.get(f)]
            if missing:
                r.fail(f"recipe missing fields: {missing}")
                return
            r.ok(f"{len(recipes)} recipe(s); first='{rec['name'][:40]}'")
    finally:
        os.unlink(pdf_path)


def test_epub_metric(r: TestResult) -> None:
    """EPUB ingest with --units metric → exit 0, archive created."""
    _, epub_path = _make_epub("Test Metric Pancakes")
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            proc = _run_cli(epub_path, "--output", out_dir, "--units", "metric")
            if proc.returncode != 0:
                r.fail(f"exit {proc.returncode}; stderr={proc.stderr.strip()[:200]}")
                return
            archives = list(Path(out_dir).glob("*.paprikarecipes"))
            if not archives:
                r.fail("no .paprikarecipes file produced with --units metric")
                return
            recipes = _read_paprikarecipes(str(archives[0]))
            if not recipes:
                r.fail("archive is empty (0 recipes)")
                return
            r.ok(f"{len(recipes)} recipe(s) with --units metric")
    finally:
        os.unlink(epub_path)


def test_merge(r: TestResult) -> None:
    """--merge of two archives → single merged archive containing recipes from both."""
    _, epub_path1 = _make_epub("Merge Pancakes")
    _, epub_path2 = _make_epub("Merge Waffles")
    try:
        with tempfile.TemporaryDirectory() as out_dir1, \
             tempfile.TemporaryDirectory() as out_dir2, \
             tempfile.TemporaryDirectory() as merge_dir:
            # Produce two separate archives
            proc1 = _run_cli(epub_path1, "--output", out_dir1)
            proc2 = _run_cli(epub_path2, "--output", out_dir2)
            if proc1.returncode != 0:
                r.fail(f"archive1 failed: exit {proc1.returncode}; {proc1.stderr[:120]}")
                return
            if proc2.returncode != 0:
                r.fail(f"archive2 failed: exit {proc2.returncode}; {proc2.stderr[:120]}")
                return
            archives1 = list(Path(out_dir1).glob("*.paprikarecipes"))
            archives2 = list(Path(out_dir2).glob("*.paprikarecipes"))
            if not archives1 or not archives2:
                r.fail("one or both source archives missing")
                return
            count1 = len(_read_paprikarecipes(str(archives1[0])))
            count2 = len(_read_paprikarecipes(str(archives2[0])))
            # Merge
            proc_merge = _run_cli(
                "--merge", str(archives1[0]), str(archives2[0]),
                "--output", merge_dir,
            )
            if proc_merge.returncode != 0:
                r.fail(f"merge exit {proc_merge.returncode}; {proc_merge.stderr[:200]}")
                return
            merged = list(Path(merge_dir).glob("*.paprikarecipes"))
            if not merged:
                r.fail("no merged archive produced")
                return
            merged_recipes = _read_paprikarecipes(str(merged[0]))
            expected = count1 + count2
            if len(merged_recipes) < expected:
                r.fail(f"merged has {len(merged_recipes)} recipes, expected ≥{expected}")
                return
            r.ok(f"merged {len(merged_recipes)} recipes from {count1}+{count2}")
    finally:
        os.unlink(epub_path1)
        os.unlink(epub_path2)


# ── Runner ───────────────────────────────────────────────────────────────────

def print_summary(results: list) -> None:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    if HAS_RICH:
        table = Table(title=f"CLI Test Results  {passed}/{total} passed")
        table.add_column("Test", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("Time", justify="right")
        table.add_column("Detail")
        for r in results:
            status = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
            table.add_row(r.name, status, f"{r.elapsed:.1f}s", r.detail)
        console.print(table)
    else:
        print(f"\nCLI Test Results  {passed}/{total} passed")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.name} ({r.elapsed:.1f}s) {r.detail}")


def run_suite() -> list:
    suite = [
        test_version,
        test_error_no_args,
        test_error_bad_extension,
        test_error_nonexistent_file,
        test_epub_ingest,
        test_pdf_ingest,
        test_epub_metric,
        test_merge,
    ]
    return [_run(fn) for fn in suite]


def main() -> int:
    results = run_suite()
    print_summary(results)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
