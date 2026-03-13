"""Shared fixtures and helpers for the recipeparser test suite."""
import sys
import types
import os
from unittest.mock import MagicMock

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
