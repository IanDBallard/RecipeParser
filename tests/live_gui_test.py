#!/usr/bin/env python3
"""
RecipeParser Live GUI Logic Tests
Run: python tests/live_gui_test.py
from RecipeParser/RecipeParser after pip install -e .

Tests only the headlessly-testable pure-Python logic in gui.py.
No display / CustomTkinter window is opened.
"""
from __future__ import annotations
import sys, time
from dataclasses import dataclass
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent


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


def _parse_run_config(free_tier: bool, concurrency_str: str):
    """Import and call gui._parse_run_config without opening a window."""
    import types, sys, importlib

    _noop = lambda *a, **k: None  # noqa: E731

    # Stub tkinter and sub-modules
    for mod_name in ("tkinter", "tkinter.ttk", "tkinter.messagebox",
                     "tkinter.filedialog", "tkinter.font"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    tk_stub = sys.modules["tkinter"]
    for attr in ("Tk", "Frame", "Label", "Button", "Entry", "StringVar",
                 "BooleanVar", "IntVar", "END", "BOTH", "LEFT", "RIGHT",
                 "TOP", "BOTTOM", "X", "Y", "W", "E", "N", "S"):
        if not hasattr(tk_stub, attr):
            setattr(tk_stub, attr, type(attr, (), {"__init__": _noop})())
    for sub in ("filedialog", "messagebox", "font", "ttk"):
        if not hasattr(tk_stub, sub):
            setattr(tk_stub, sub, types.ModuleType(sub))

    # Stub customtkinter — module-level calls (set_appearance_mode, etc.)
    # must be plain callables, not class instances.
    if "customtkinter" not in sys.modules:
        ctk_stub = types.ModuleType("customtkinter")
        sys.modules["customtkinter"] = ctk_stub
    else:
        ctk_stub = sys.modules["customtkinter"]

    # Functions called at module level in gui.py — must be callable
    for fn_attr in ("set_appearance_mode", "set_default_color_theme"):
        if not hasattr(ctk_stub, fn_attr):
            setattr(ctk_stub, fn_attr, _noop)

    # Widget classes and variable types — complete list from gui.py
    for cls_attr in ("CTk", "CTkFrame", "CTkLabel", "CTkButton", "CTkEntry",
                     "CTkCheckBox", "CTkOptionMenu", "CTkScrollableFrame",
                     "CTkProgressBar", "CTkTextbox", "CTkToplevel",
                     "CTkTabview", "CTkFont", "BooleanVar", "StringVar"):
        if not hasattr(ctk_stub, cls_attr):
            setattr(ctk_stub, cls_attr,
                    type(cls_attr, (), {"__init__": _noop}))

    # Import (or reuse cached) gui module
    if "recipeparser.gui" in sys.modules:
        gui = sys.modules["recipeparser.gui"]
    else:
        sys.path.insert(0, str(REPO_ROOT))
        gui = importlib.import_module("recipeparser.gui")

    return gui._parse_run_config(free_tier, concurrency_str)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_free_tier_config(r: TestResult) -> None:
    """free_tier=True → rpm=5, concurrency=1 regardless of concurrency_str."""
    rpm, conc = _parse_run_config(True, "1")
    if rpm != 5:
        r.fail(f"expected rpm=5, got {rpm}")
        return
    if conc != 1:
        r.fail(f"expected concurrency=1, got {conc}")
        return
    r.ok(f"rpm={rpm}, concurrency={conc}")


def test_paid_tier_config(r: TestResult) -> None:
    """free_tier=False, concurrency_str=3 → rpm=None, concurrency=3."""
    rpm, conc = _parse_run_config(False, "3")
    if rpm is not None:
        r.fail(f"expected rpm=None, got {rpm}")
        return
    if conc != 3:
        r.fail(f"expected concurrency=3, got {conc}")
        return
    r.ok(f"rpm={rpm}, concurrency={conc}")


def test_concurrency_clamp_low(r: TestResult) -> None:
    """free_tier=False, concurrency_str=0 → clamped to 1."""
    rpm, conc = _parse_run_config(False, "0")
    if conc != 1:
        r.fail(f"expected concurrency clamped to 1, got {conc}")
        return
    r.ok(f"concurrency clamped low → {conc}")


def test_concurrency_clamp_high(r: TestResult) -> None:
    """free_tier=False, concurrency_str=11 → clamped to 10."""
    rpm, conc = _parse_run_config(False, "11")
    if conc != 10:
        r.fail(f"expected concurrency clamped to 10, got {conc}")
        return
    r.ok(f"concurrency clamped high → {conc}")


# ── Runner ───────────────────────────────────────────────────────────────────

def print_summary(results: list) -> None:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    if HAS_RICH:
        table = Table(title=f"GUI Logic Test Results  {passed}/{total} passed")
        table.add_column("Test", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("Time", justify="right")
        table.add_column("Detail")
        for r in results:
            status = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
            table.add_row(r.name, status, f"{r.elapsed:.3f}s", r.detail)
        console.print(table)
    else:
        print(f"\nGUI Logic Test Results  {passed}/{total} passed")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.name} ({r.elapsed:.3f}s) {r.detail}")


def run_suite() -> list:
    suite = [
        test_free_tier_config,
        test_paid_tier_config,
        test_concurrency_clamp_low,
        test_concurrency_clamp_high,
    ]
    return [_run(fn) for fn in suite]


def main() -> int:
    results = run_suite()
    print_summary(results)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
