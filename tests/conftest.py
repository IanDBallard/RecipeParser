"""Shared fixtures and helpers for the recipeparser test suite."""
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


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


#: (option attribute, flag, why a distributed run would be wrong).  Order is
#: the order they are reported in; the first match wins.
_SERIAL_ONLY = (
    (
        "record_gemini",
        "--record-gemini",
        "GlobalRateLimiter is a per-process singleton, so N xdist workers would "
        "issue N x the intended RPM against the real Gemini quota",
    ),
    (
        "update_goldens",
        "--update-goldens",
        "a golden regenerated under load bakes any flake into a committed file",
    ),
    (
        "update_snapshots",
        "--snapshot-update",
        "xdist suppresses syrupy's snapshot report, which is what detects a "
        "stale or unused snapshot",
    ),
)


def serial_reason(option) -> "str | None":
    """Why this run must not be distributed across xdist workers, or None.

    Pure on purpose: the whole policy is one readable table plus this lookup,
    so tests/unit/test_pytest_config.py can check it without spawning pytest.
    Attributes are read defensively — syrupy or xdist may not be installed.
    """
    for attr, flag, why in _SERIAL_ONLY:
        if getattr(option, attr, False):
            return f"{flag}: {why}"
    return None


#: Most workers worth starting.  Measured on the 759-test suite (8-core/16-thread):
#: serial 9.23s, n=4 7.18s, n=6 7.18s, n=8 7.50s, n=16 10.14s.  Past this the
#: per-worker import cost outruns the gain -- which is why xdist's own "auto" is
#: not used here: with no psutil it counts *logical* cores (16 on this desktop)
#: and lands slower than not distributing at all.
WORKER_CAP = 6


def default_workers(cpu_count: "int | None") -> int:
    """How many xdist workers to use when the operator named no count.

    Returns 0 -- serial -- for a machine that cannot gain from workers, so a
    single-core runner never pays startup for parallelism it cannot use.
    """
    if not cpu_count or cpu_count < 2:
        return 0
    return min(WORKER_CAP, cpu_count)


#: Set when pytest_configure demoted a distributed run, so the header can say so.
_FORCED_SERIAL = pytest.StashKey[str]()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: "pytest.Config") -> None:
    """Demote a write-mode run to serial before xdist reads its own options.

    pyproject.toml sets ``addopts = -n auto``, so the guarded flags would
    otherwise inherit the parallel default.  tryfirst is what makes this work:
    xdist decides whether to distribute in its own pytest_configure.
    """
    if not hasattr(config.option, "numprocesses"):
        return  # xdist not installed; nothing to decide

    reason = serial_reason(config.option)
    if reason:
        if config.option.numprocesses:
            config.option.numprocesses = 0
            config.option.dist = "no"
            config.stash[_FORCED_SERIAL] = reason
        return



def pytest_xdist_auto_num_workers(config: "pytest.Config") -> int:
    """Resolve the ``-n auto`` in pyproject.toml to a count that suits the host.

    xdist's own answer is os.cpu_count() -- 16 logical on the desktop this was
    tuned on, which measured slower than serial.  Capping it is the whole point;
    see WORKER_CAP for the numbers.

    This is the hook xdist provides for the job, and it has to be: distribution
    cannot be switched on from pytest_configure, because xdist has already built
    config.option.tx from the worker count by then.
    """
    return default_workers(os.cpu_count())


def pytest_report_header(config: "pytest.Config") -> "str | None":
    reason = config.stash.get(_FORCED_SERIAL, None)
    return f"forcing serial execution -- {reason}" if reason else None


# ──────────────────────────────────────────────────────────────────────────────
# Headless tkinter / customtkinter stubs
# Injected into sys.modules BEFORE any test module imports recipeparser.gui so
# that the GUI module can be imported in environments without a display or the
# tkinter C extension (e.g. PlatformIO's embedded Python).
# ──────────────────────────────────────────────────────────────────────────────

def _make_tkinter_stub() -> types.ModuleType:
    """Return a minimal tkinter stub that satisfies gui.py's import surface."""
    tk = types.ModuleType("tkinter")

    # filedialog / messagebox sub-modules
    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = MagicMock(return_value="")
    filedialog.asksaveasfilename = MagicMock(return_value="")
    filedialog.askdirectory = MagicMock(return_value="")

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.showinfo = MagicMock()
    messagebox.showwarning = MagicMock()
    messagebox.showerror = MagicMock()
    messagebox.askyesno = MagicMock(return_value=False)

    tk.filedialog = filedialog
    tk.messagebox = messagebox

    # Minimal Tk base classes used as parents in gui.py
    class _Widget:
        def __init__(self, *a, **kw): pass
        def grid(self, *a, **kw): pass
        def pack(self, *a, **kw): pass
        def configure(self, *a, **kw): pass
        def bind(self, *a, **kw): pass
        def winfo_children(self): return []
        def destroy(self): pass
        def after(self, *a, **kw): pass
        def get(self): return ""
        def insert(self, *a, **kw): pass
        def focus(self): pass
        def set(self, *a, **kw): pass
        def delete(self, *a, **kw): pass
        def see(self, *a, **kw): pass
        def grab_set(self): pass
        def wait_window(self): pass
        def resizable(self, *a, **kw): pass
        def title(self, *a, **kw): pass
        def geometry(self, *a, **kw): pass
        def minsize(self, *a, **kw): pass
        def mainloop(self): pass
        def protocol(self, *a, **kw): pass
        def grid_columnconfigure(self, *a, **kw): pass
        def grid_rowconfigure(self, *a, **kw): pass

    tk.Tk = _Widget
    tk.Toplevel = _Widget
    tk.Frame = _Widget
    tk.StringVar = _Widget
    tk.BooleanVar = _Widget

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.filedialog"] = filedialog
    sys.modules["tkinter.messagebox"] = messagebox
    return tk


def _make_customtkinter_stub() -> types.ModuleType:
    """Return a minimal customtkinter stub that satisfies gui.py's import surface."""
    ctk = types.ModuleType("customtkinter")

    class _W:
        """Generic widget stub — accepts any args/kwargs, ignores them."""
        def __init__(self, *a, **kw): pass
        def grid(self, *a, **kw): pass
        def pack(self, *a, **kw): pass
        def configure(self, *a, **kw): pass
        def bind(self, *a, **kw): pass
        def winfo_children(self): return []
        def destroy(self): pass
        def after(self, *a, **kw): pass
        def get(self): return ""
        def insert(self, *a, **kw): pass
        def focus(self): pass
        def set(self, *a, **kw): pass
        def delete(self, *a, **kw): pass
        def see(self, *a, **kw): pass
        def grab_set(self): pass
        def wait_window(self): pass
        def resizable(self, *a, **kw): pass
        def title(self, *a, **kw): pass
        def geometry(self, *a, **kw): pass
        def minsize(self, *a, **kw): pass
        def mainloop(self): pass
        def protocol(self, *a, **kw): pass
        def grid_columnconfigure(self, *a, **kw): pass
        def grid_rowconfigure(self, *a, **kw): pass
        def tab(self, *a, **kw): return _W()
        def add(self, *a, **kw): pass

    ctk.CTk = _W
    ctk.CTkFrame = _W
    ctk.CTkToplevel = _W
    ctk.CTkLabel = _W
    ctk.CTkButton = _W
    ctk.CTkEntry = _W
    ctk.CTkTextbox = _W
    ctk.CTkCheckBox = _W
    ctk.CTkOptionMenu = _W
    ctk.CTkScrollableFrame = _W
    ctk.CTkProgressBar = _W
    ctk.CTkTabview = _W
    ctk.CTkFont = _W
    ctk.StringVar = _W
    ctk.BooleanVar = _W
    ctk.set_appearance_mode = MagicMock()
    ctk.set_default_color_theme = MagicMock()

    sys.modules["customtkinter"] = ctk
    return ctk


# Only inject stubs when tkinter is genuinely unavailable.
if "tkinter" not in sys.modules:
    try:
        import tkinter as _tk_real  # noqa: F401
    except ModuleNotFoundError:
        _make_tkinter_stub()
        _make_customtkinter_stub()

# ──────────────────────────────────────────────────────────────────────────────

# Ensure a dummy API key exists so __init__.py can construct the client
# without a real .env file present.
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")

# The API refuses to boot with DISABLE_AUTH set but no UUID TEST_USER_ID, so
# supply one here for every test module that imports recipeparser.adapters.api
# (not just tests/test_api.py, whichever pytest collects first).
if os.environ.get("DISABLE_AUTH", "0").strip().lower() in {"1", "true", "yes", "on"}:
    os.environ.setdefault("TEST_USER_ID", "00000000-0000-0000-0000-000000000001")

from recipeparser.models import RecipeExtraction  # noqa: E402 (env must be set first)


def make_recipe(name: str, photo: str | None = None) -> RecipeExtraction:
    return RecipeExtraction(
        name=name,
        photo_filename=photo,
        ingredients=["1 cup flour", "1/2 tsp salt"],
        directions=["Mix ingredients.", "Bake at 350F for 30 mins."],
    )


def make_mock_client(return_value=None, side_effect=None):
    """Return a minimal mock of google.genai.Client with generate_content configured."""
    client = MagicMock()
    if side_effect is not None:
        client.models.generate_content.side_effect = side_effect
    else:
        client.models.generate_content.return_value = return_value
    return client
