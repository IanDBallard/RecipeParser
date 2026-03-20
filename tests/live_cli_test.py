#!/usr/bin/env python3
"""
RecipeParser Live CLI Tests
Run: python tests/live_cli_test.py
from RecipeParser/RecipeParser after pip install -e .
Requires GOOGLE_API_KEY in environment or .env file.
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
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
# Ensure tests.fixtures is importable when run as python tests/live_cli_test.py
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from tests.fixtures import make_epub, make_pdf, read_paprikarecipes

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
    _, epub_path = make_epub("Test Pancakes")
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
            recipes = read_paprikarecipes(str(archives[0]))
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
    _, pdf_path = make_pdf("Test Beef Stew")
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
            recipes = read_paprikarecipes(str(archives[0]))
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
    _, epub_path = make_epub("Test Metric Pancakes")
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
            recipes = read_paprikarecipes(str(archives[0]))
            if not recipes:
                r.fail("archive is empty (0 recipes)")
                return
            r.ok(f"{len(recipes)} recipe(s) with --units metric")
    finally:
        os.unlink(epub_path)


def test_merge(r: TestResult) -> None:
    """--merge of two archives → single merged archive containing recipes from both."""
    _, epub_path1 = make_epub("Merge Pancakes")
    _, epub_path2 = make_epub("Merge Waffles")
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
            count1 = len(read_paprikarecipes(str(archives1[0])))
            count2 = len(read_paprikarecipes(str(archives2[0])))
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
            merged_recipes = read_paprikarecipes(str(merged[0]))
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
