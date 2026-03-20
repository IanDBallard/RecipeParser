#!/usr/bin/env python3
"""
RecipeParser Live GUI E2E Tests

Full end-to-end tests for the Parse tab: set paths, invoke Parse, wait for
pipeline completion (real Gemini API), verify output.

Run: python tests/live_gui_e2e.py
from RecipeParser/RecipeParser after pip install -e .

Requires GOOGLE_API_KEY in environment or .env file.
Requires a display (or xvfb-run on Linux).

Gate with RECIPEPARSER_LIVE_GUI=1 to run (avoids CI; needs display + Gemini).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.fixtures import make_epub, read_paprikarecipes

RUN_LIVE = os.environ.get("RECIPEPARSER_LIVE_GUI", "0") == "1"

try:
    import tkinter  # noqa: F401
    HAS_TK = True
except ModuleNotFoundError:
    HAS_TK = False


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


@dataclass
class E2EState:
    """Shared state for the parse-flow E2E test."""
    result_path: str | None = None
    error: str | None = None
    done: bool = False


def _run(fn) -> TestResult:
    r = TestResult(name=fn.__name__)
    t0 = time.time()
    try:
        fn(r)
    except Exception as exc:
        r.fail(f"{type(exc).__name__}: {exc}")
    r.elapsed = time.time() - t0
    return r


def _make_patch_context(epub_path: str, output_dir: str):
    """Return a context manager that mocks filedialog and messagebox for headless E2E."""
    def mock_askopenfilename(**kwargs):
        return epub_path

    def mock_askdirectory(**kwargs):
        return output_dir

    def mock_asksaveasfilename(**kwargs):
        return str(Path(output_dir) / "categories.yaml")

    def mock_noop(*args, **kwargs):
        pass

    def mock_askyesno_no(*args, **kwargs):
        return False

    from contextlib import contextmanager

    @contextmanager
    def _patched():
        with patch.multiple(
            "tkinter.filedialog",
            askopenfilename=mock_askopenfilename,
            askdirectory=mock_askdirectory,
            asksaveasfilename=mock_asksaveasfilename,
        ), patch.multiple(
            "tkinter.messagebox",
            showwarning=mock_noop,
            showerror=mock_noop,
            showinfo=mock_noop,
            askyesno=mock_askyesno_no,
        ):
            yield

    return _patched()


def _run_parse_e2e(r: TestResult) -> None:
    """EPUB parse via GUI: set paths, click Parse, wait for completion, verify output."""
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        r.fail("GOOGLE_API_KEY not set")
        return

    _, epub_path = make_epub("GUI E2E Pancakes")
    try:
        with tempfile.TemporaryDirectory() as output_dir:
            with _make_patch_context(epub_path, output_dir):
                from recipeparser.adapters.gui import create_app_for_test

            app = create_app_for_test()
            state = E2EState()

            def _start_parse():
                pf = app._parse_frame
                pf._epub_var.set(epub_path)
                pf._output_var.set(output_dir)
                pf._api_frame._key_var.set(api_key)
                pf._parse_btn.invoke()
                app.after(2000, lambda: _poll(state, output_dir))

            def _poll(state: E2EState, output_dir: str):
                pf = app._parse_frame
                if state.done:
                    return
                status = pf._status_var.get()
                if pf._running:
                    app.after(2000, lambda: _poll(state, output_dir))
                    return
                if "Done" in status:
                    archives = list(Path(output_dir).glob("*.paprikarecipes"))
                    if archives:
                        state.result_path = str(archives[0])
                    state.done = True
                    app.quit()
                elif "error" in status.lower() or "Error" in status:
                    state.error = status
                    state.done = True
                    app.quit()
                else:
                    app.after(2000, lambda: _poll(state, output_dir))

            app.after(0, _start_parse)
            app.mainloop()

            if state.error:
                log_text = ""
                try:
                    log_text = app._parse_frame._log_box.get("1.0", "end").strip()
                except Exception:
                    pass
                if log_text:
                    print("\n--- Parse log (full) ---\n", log_text, "\n---\n", file=sys.stderr)
                    r.fail(f"{state.error}\n(See stderr for full log)")
                else:
                    r.fail(state.error)
                return
            if not state.result_path:
                r.fail("No .paprikarecipes file produced")
                return
            recipes = read_paprikarecipes(state.result_path)
            if not recipes:
                r.fail("Archive is empty (0 recipes)")
                return
            rec = recipes[0]
            missing = [f for f in ("name", "ingredients", "directions") if not rec.get(f)]
            if missing:
                r.fail(f"Recipe missing fields: {missing}")
                return
            r.ok(f"{len(recipes)} recipe(s); first='{rec['name'][:40]}'")
    finally:
        try:
            os.unlink(epub_path)
        except OSError:
            pass


def test_parse_epub_full_flow(r: TestResult) -> None:
    """GUI Parse tab: EPUB ingest via Parse button, real Gemini, verify .paprikarecipes."""
    _run_parse_e2e(r)


def print_summary(results: list) -> None:
    passed = sum(1 for x in results if x.passed)
    total = len(results)
    if HAS_RICH:
        table = Table(title=f"GUI E2E Results  {passed}/{total} passed")
        table.add_column("Test", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("Time", justify="right")
        table.add_column("Detail")
        for x in results:
            status = "[green]PASS[/green]" if x.passed else "[red]FAIL[/red]"
            table.add_row(x.name, status, f"{x.elapsed:.1f}s", x.detail)
        console.print(table)
    else:
        print(f"\nGUI E2E Results  {passed}/{total} passed")
        for x in results:
            status = "PASS" if x.passed else "FAIL"
            print(f"  [{status}] {x.name} ({x.elapsed:.1f}s) {x.detail}")


def run_suite() -> list:
    suite = [test_parse_epub_full_flow]
    return [_run(fn) for fn in suite]


def main() -> int:
    if not RUN_LIVE:
        print("Skipping GUI E2E (set RECIPEPARSER_LIVE_GUI=1 to run)")
        return 0
    if not HAS_TK:
        print("Skipping GUI E2E (tkinter not available; needs display)")
        return 0
    results = run_suite()
    print_summary(results)
    return 0 if all(x.passed for x in results) else 1


if __name__ == "__main__":
    sys.exit(main())
