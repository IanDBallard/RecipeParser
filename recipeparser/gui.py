"""
RecipeParser GUI — CustomTkinter front-end for the recipe parsing pipeline.

Entry point: run_gui()
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import yaml
from tkinter import filedialog, messagebox
from importlib.metadata import version as _pkg_version, PackageNotFoundError

from recipeparser.categories import _CATEGORIES_FILE, load_category_tree
from recipeparser.paths import get_default_output_dir, get_env_file

# ──────────────────────────────────────────────────────────────────────────────
# Theme
# ──────────────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

APP_TITLE = "RecipeParser"
try:
    APP_VERSION = _pkg_version("recipeparser")
except PackageNotFoundError:
    APP_VERSION = "dev"

UNITS_OPTIONS = ["book", "metric", "us", "imperial"]
UNITS_LABELS = {
    "book": "Book default",
    "metric": "Metric (g / ml)",
    "us": "US (cups / tbsp)",
    "imperial": "Imperial (oz / lb)",
}


def _parse_run_config(free_tier: bool, concurrency_str: str) -> tuple[Optional[int], int]:
    """
    Compute (rpm_val, concurrency_val) for the pipeline from Parse frame state.
    Free tier → rpm=5, concurrency=1; otherwise no RPM cap and concurrency clamped 1–10.
    """
    if free_tier:
        return (5, 1)
    concurrency_val = min(10, max(1, int(concurrency_str)))
    return (None, concurrency_val)


# ──────────────────────────────────────────────────────────────────────────────
# Logging handler that feeds a queue consumed by the GUI log panel
# ──────────────────────────────────────────────────────────────────────────────

class _QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self._queue = log_queue

    def emit(self, record: logging.LogRecord):
        self._queue.put(self.format(record))


# ──────────────────────────────────────────────────────────────────────────────
# Category editor frame
# ──────────────────────────────────────────────────────────────────────────────

class CategoryEditorFrame(ctk.CTkFrame):
    """Two-panel category editor: parent list on the left, children on the right."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._dirty = False
        self._data: dict[str, list[str]] = {}   # parent -> [child, ...]
        self._selected_parent: Optional[str] = None

        self._build_ui()
        self._load()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)

        # ── Left panel: parent categories ─────────────────────────────────────
        left = ctk.CTkFrame(self)
        left.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="nsew")
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left, text="Categories", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="w"
        )

        self._parent_list = ctk.CTkScrollableFrame(left)
        self._parent_list.grid(row=1, column=0, columnspan=2, padx=8, pady=4, sticky="nsew")
        self._parent_list.grid_columnconfigure(0, weight=1)

        btn_row = ctk.CTkFrame(left, fg_color="transparent")
        btn_row.grid(row=2, column=0, columnspan=2, padx=8, pady=(4, 8), sticky="ew")
        btn_row.grid_columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkButton(btn_row, text="＋", width=36, command=self._add_parent).grid(row=0, column=0, padx=2)
        ctk.CTkButton(btn_row, text="✎", width=36, command=self._rename_parent).grid(row=0, column=1, padx=2)
        ctk.CTkButton(btn_row, text="↑", width=36, command=lambda: self._move_parent(-1)).grid(row=0, column=2, padx=2)
        ctk.CTkButton(btn_row, text="↓", width=36, command=lambda: self._move_parent(1)).grid(row=0, column=3, padx=2)
        ctk.CTkButton(btn_row, text="✕", width=36, fg_color="#c0392b", hover_color="#922b21",
                      command=self._delete_parent).grid(row=0, column=4, padx=2)

        # ── Right panel: sub-categories ────────────────────────────────────────
        right = ctk.CTkFrame(self)
        right.grid(row=0, column=1, padx=(4, 8), pady=8, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self._child_header = ctk.CTkLabel(
            right, text="Subcategories", font=ctk.CTkFont(size=13, weight="bold")
        )
        self._child_header.grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="w")

        self._child_list = ctk.CTkScrollableFrame(right)
        self._child_list.grid(row=1, column=0, columnspan=2, padx=8, pady=4, sticky="nsew")
        self._child_list.grid_columnconfigure(0, weight=1)

        btn_row2 = ctk.CTkFrame(right, fg_color="transparent")
        btn_row2.grid(row=2, column=0, columnspan=2, padx=8, pady=(4, 8), sticky="ew")
        btn_row2.grid_columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkButton(btn_row2, text="＋", width=36, command=self._add_child).grid(row=0, column=0, padx=2)
        ctk.CTkButton(btn_row2, text="✎", width=36, command=self._rename_child).grid(row=0, column=1, padx=2)
        ctk.CTkButton(btn_row2, text="↑", width=36, command=lambda: self._move_child(-1)).grid(row=0, column=2, padx=2)
        ctk.CTkButton(btn_row2, text="↓", width=36, command=lambda: self._move_child(1)).grid(row=0, column=3, padx=2)
        ctk.CTkButton(btn_row2, text="✕", width=36, fg_color="#c0392b", hover_color="#922b21",
                      command=self._delete_child).grid(row=0, column=4, padx=2)

        # ── Bottom toolbar ─────────────────────────────────────────────────────
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=1, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="ew")
        bottom.grid_columnconfigure(3, weight=1)

        ctk.CTkButton(bottom, text="Import YAML…", command=self._import_yaml).grid(row=0, column=0, padx=4)
        ctk.CTkButton(bottom, text="Export YAML…", command=self._export_yaml).grid(row=0, column=1, padx=4)
        ctk.CTkButton(bottom, text="Sync from Paprika…", command=self._sync_from_paprika).grid(row=0, column=2, padx=4)
        self._save_btn = ctk.CTkButton(
            bottom, text="Save Changes", fg_color="#27ae60", hover_color="#1e8449",
            command=self._save
        )
        self._save_btn.grid(row=0, column=4, padx=4)

        self.grid_rowconfigure(1, weight=0)

    # ── Data model helpers ─────────────────────────────────────────────────────

    def _load(self, path: Path = _CATEGORIES_FILE):
        """Read the YAML and populate internal data structures."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            entries = raw.get("categories", [])
        except Exception:
            entries = []

        self._data = {}
        self._order: list[str] = []   # preserves parent insertion order

        for entry in entries:
            if isinstance(entry, str):
                self._data[entry] = []
                self._order.append(entry)
            elif isinstance(entry, dict):
                for parent, children in entry.items():
                    self._data[parent] = [str(c) for c in (children or [])]
                    self._order.append(parent)

        self._dirty = False
        self._refresh_parents()

    def _to_yaml_structure(self) -> dict:
        entries = []
        for parent in self._order:
            children = self._data.get(parent, [])
            if children:
                entries.append({parent: children})
            else:
                entries.append(parent)
        return {"categories": entries}

    # ── Rendering ──────────────────────────────────────────────────────────────

    _NORMAL_COLOR = ("#3B8ED0", "#1F6AA5")
    _SELECTED_COLOR = ("#1a5276", "#1a5276")

    def _refresh_parents(self):
        for w in self._parent_list.winfo_children():
            w.destroy()
        for i, parent in enumerate(self._order):
            color = self._SELECTED_COLOR if parent == self._selected_parent else self._NORMAL_COLOR
            btn = ctk.CTkButton(
                self._parent_list,
                text=parent,
                anchor="w",
                fg_color=color,
                command=lambda p=parent: self._select_parent(p),
            )
            btn.grid(row=i, column=0, padx=4, pady=2, sticky="ew")
        self._refresh_children()

    def _refresh_children(self):
        for w in self._child_list.winfo_children():
            w.destroy()

        if self._selected_parent is None:
            self._child_header.configure(text="Subcategories")
            return

        self._child_header.configure(text=f"Subcategories of  '{self._selected_parent}'")
        children = self._data.get(self._selected_parent, [])
        for i, child in enumerate(children):
            row_frame = ctk.CTkFrame(self._child_list, fg_color="transparent")
            row_frame.grid(row=i, column=0, padx=4, pady=2, sticky="ew")
            row_frame.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row_frame, text=child, anchor="w").grid(row=0, column=0, sticky="ew", padx=4)

    def _select_parent(self, parent: str):
        self._selected_parent = parent
        self._refresh_parents()

    # ── Parent mutations ───────────────────────────────────────────────────────

    def _add_parent(self):
        name = self._prompt("New category name:")
        if name and name not in self._data:
            self._data[name] = []
            self._order.append(name)
            self._dirty = True
            self._selected_parent = name
            self._refresh_parents()

    def _rename_parent(self):
        if not self._selected_parent:
            return
        new_name = self._prompt("Rename category:", default=self._selected_parent)
        if new_name and new_name != self._selected_parent and new_name not in self._data:
            idx = self._order.index(self._selected_parent)
            children = self._data.pop(self._selected_parent)
            self._data[new_name] = children
            self._order[idx] = new_name
            self._selected_parent = new_name
            self._dirty = True
            self._refresh_parents()

    def _delete_parent(self):
        if not self._selected_parent:
            return
        if messagebox.askyesno("Delete", f"Delete '{self._selected_parent}' and all its subcategories?"):
            self._order.remove(self._selected_parent)
            del self._data[self._selected_parent]
            self._selected_parent = self._order[0] if self._order else None
            self._dirty = True
            self._refresh_parents()

    def _move_parent(self, direction: int):
        if not self._selected_parent:
            return
        idx = self._order.index(self._selected_parent)
        new_idx = idx + direction
        if 0 <= new_idx < len(self._order):
            self._order[idx], self._order[new_idx] = self._order[new_idx], self._order[idx]
            self._dirty = True
            self._refresh_parents()

    # ── Child mutations ────────────────────────────────────────────────────────

    def _add_child(self):
        if not self._selected_parent:
            messagebox.showinfo("No category selected", "Select a parent category first.")
            return
        name = self._prompt("New subcategory name:")
        if name and name not in self._data[self._selected_parent]:
            self._data[self._selected_parent].append(name)
            self._dirty = True
            self._refresh_children()

    def _rename_child(self):
        if not self._selected_parent:
            return
        children = self._data[self._selected_parent]
        if not children:
            return
        # pick first child whose label widget is "active" — simpler: ask user to type name
        old = self._prompt("Current subcategory name to rename:")
        if old not in children:
            messagebox.showwarning("Not found", f"'{old}' is not a subcategory of '{self._selected_parent}'.")
            return
        new = self._prompt("New name:", default=old)
        if new and new != old:
            idx = children.index(old)
            children[idx] = new
            self._dirty = True
            self._refresh_children()

    def _delete_child(self):
        if not self._selected_parent:
            return
        children = self._data[self._selected_parent]
        if not children:
            return
        name = self._prompt("Subcategory name to delete:")
        if name in children:
            children.remove(name)
            self._dirty = True
            self._refresh_children()

    def _move_child(self, direction: int):
        if not self._selected_parent:
            return
        children = self._data[self._selected_parent]
        name = self._prompt("Subcategory name to move:")
        if name not in children:
            return
        idx = children.index(name)
        new_idx = idx + direction
        if 0 <= new_idx < len(children):
            children[idx], children[new_idx] = children[new_idx], children[idx]
            self._dirty = True
            self._refresh_children()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _save(self, path: Path = _CATEGORIES_FILE):
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(
                    self._to_yaml_structure(),
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
            self._dirty = False
            messagebox.showinfo("Saved", f"Categories saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _import_yaml(self):
        path = filedialog.askopenfilename(
            title="Import categories YAML",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if path:
            self._load(Path(path))
            self._dirty = True

    def _export_yaml(self):
        path = filedialog.asksaveasfilename(
            title="Export categories YAML",
            defaultextension=".yaml",
            filetypes=[("YAML files", "*.yaml *.yml")],
        )
        if path:
            self._save(Path(path))

    def _sync_from_paprika(self):
        from recipeparser.paprika_db import find_paprika_db, read_categories_from_db

        db = find_paprika_db()
        if not db:
            messagebox.showwarning(
                "Paprika Not Found",
                "Could not locate Paprika.sqlite on this computer.\n\n"
                "Make sure Paprika 3 is installed and has been opened at least once.",
            )
            return

        if not messagebox.askyesno(
            "Sync from Paprika",
            f"Replace the current category list with the live taxonomy from:\n\n"
            f"{db}\n\n"
            "The editor will be marked as having unsaved changes.\n"
            "Proceed?",
        ):
            return

        try:
            data, order = read_categories_from_db(db)
        except Exception as e:
            messagebox.showerror("Sync Failed", str(e))
            return

        if not order:
            messagebox.showwarning("No Categories", "Paprika returned no categories.")
            return

        self._data = data
        self._order = order
        self._selected_parent = order[0]
        self._dirty = True
        self._refresh_parents()
        messagebox.showinfo(
            "Sync Complete",
            f"Loaded {len(order)} top-level categories from Paprika.\n\n"
            "Review the list, then click 'Save Changes' to persist to categories.yaml.",
        )

    # ── Utility ────────────────────────────────────────────────────────────────

    @staticmethod
    def _prompt(label: str, default: str = "") -> Optional[str]:
        dlg = _InputDialog(label, default)
        return dlg.result

    def has_unsaved_changes(self) -> bool:
        return self._dirty


# ──────────────────────────────────────────────────────────────────────────────
# Simple input dialog (CustomTkinter doesn't ship one)
# ──────────────────────────────────────────────────────────────────────────────

class _InputDialog(ctk.CTkToplevel):
    def __init__(self, prompt: str, default: str = ""):
        super().__init__()
        self.result: Optional[str] = None
        self.title("Input")
        self.resizable(False, False)
        self.grab_set()

        ctk.CTkLabel(self, text=prompt).pack(padx=20, pady=(16, 4))
        self._entry = ctk.CTkEntry(self, width=260)
        self._entry.pack(padx=20, pady=4)
        self._entry.insert(0, default)
        self._entry.focus()

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(padx=20, pady=(4, 16))
        ctk.CTkButton(btn_row, text="OK", width=100, command=self._ok).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="Cancel", width=100, command=self.destroy).pack(side="left", padx=4)

        self._entry.bind("<Return>", lambda _: self._ok())
        self._entry.bind("<Escape>", lambda _: self.destroy())

        self.wait_window()

    def _ok(self):
        self.result = self._entry.get().strip() or None
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# Settings / API key frame (shown inside the Parse tab)
# ──────────────────────────────────────────────────────────────────────────────

def _get_env_file() -> Path:
    """Return the .env path (always user-writable app data dir)."""
    return get_env_file()


class _ApiKeyFrame(ctk.CTkFrame):
    """Small collapsible section for API key management."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text="Google API Key", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self._key_var = ctk.StringVar(value=self._read_key())
        self._entry = ctk.CTkEntry(self, textvariable=self._key_var, show="●", width=300)
        self._entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ctk.CTkButton(self, text="Save", width=70, command=self._save_key).grid(row=0, column=2)

    def _read_key(self) -> str:
        env = _get_env_file()
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("GOOGLE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        return os.environ.get("GOOGLE_API_KEY", "")

    def _save_key(self):
        key = self._key_var.get().strip()
        if not key:
            messagebox.showwarning("API Key", "Please enter a valid API key.")
            return
        env = _get_env_file()
        lines = env.read_text().splitlines() if env.exists() else []
        new_lines = [ln for ln in lines if not ln.startswith("GOOGLE_API_KEY=")]
        new_lines.append(f"GOOGLE_API_KEY={key}")
        env.write_text("\n".join(new_lines) + "\n")
        os.environ["GOOGLE_API_KEY"] = key
        messagebox.showinfo("Saved", f"API key saved to {env}")

    def get_key(self) -> str:
        return self._key_var.get().strip()


# ──────────────────────────────────────────────────────────────────────────────
# Parse tab
# ──────────────────────────────────────────────────────────────────────────────

class ParseFrame(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._log_queue: queue.Queue = queue.Queue()
        self._running = False

        self._build_ui()
        self._poll_log()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        pad = {"padx": 16, "pady": 6}

        # ── API key ────────────────────────────────────────────────────────────
        self._api_frame = _ApiKeyFrame(self)
        self._api_frame.grid(row=0, column=0, sticky="ew", **pad)

        ctk.CTkFrame(self, height=1, fg_color=("gray70", "gray30")).grid(
            row=1, column=0, sticky="ew", padx=16, pady=2
        )

        # ── Inputs ─────────────────────────────────────────────────────────────
        inputs = ctk.CTkFrame(self, fg_color="transparent")
        inputs.grid(row=2, column=0, sticky="ew", **pad)
        inputs.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(inputs, text="EPUB File", width=90, anchor="w").grid(row=0, column=0, sticky="w", pady=4)
        self._epub_var = ctk.StringVar()
        ctk.CTkEntry(inputs, textvariable=self._epub_var).grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ctk.CTkButton(inputs, text="Browse…", width=90, command=self._browse_epub).grid(row=0, column=2)

        ctk.CTkLabel(inputs, text="Output Folder", width=90, anchor="w").grid(row=1, column=0, sticky="w", pady=4)
        self._output_var = ctk.StringVar(value=str(get_default_output_dir()))
        ctk.CTkEntry(inputs, textvariable=self._output_var).grid(row=1, column=1, sticky="ew", padx=(0, 8))
        ctk.CTkButton(inputs, text="Browse…", width=90, command=self._browse_output).grid(row=1, column=2)

        ctk.CTkLabel(inputs, text="Units", width=90, anchor="w").grid(row=2, column=0, sticky="w", pady=4)
        self._units_var = ctk.StringVar(value="book")
        units_menu = ctk.CTkOptionMenu(
            inputs,
            variable=self._units_var,
            values=list(UNITS_LABELS.values()),
            command=self._on_units_change,
        )
        units_menu.grid(row=2, column=1, sticky="w")
        # keep display labels but store canonical values
        self._label_to_unit = {v: k for k, v in UNITS_LABELS.items()}
        units_menu.set(UNITS_LABELS["book"])

        ctk.CTkLabel(inputs, text="API rate limit", width=90, anchor="w").grid(row=3, column=0, sticky="w", pady=4)
        self._free_tier_var = ctk.BooleanVar(value=True)
        free_tier_cb = ctk.CTkCheckBox(
            inputs, text="Free tier (5 req/min)", variable=self._free_tier_var,
            command=self._on_free_tier_change,
        )
        free_tier_cb.grid(row=3, column=1, sticky="w")

        ctk.CTkLabel(inputs, text="Concurrency", width=90, anchor="w").grid(row=4, column=0, sticky="w", pady=4)
        self._concurrency_var = ctk.StringVar(value="1")
        self._concurrency_spin = ctk.CTkOptionMenu(
            inputs, variable=self._concurrency_var,
            values=[str(i) for i in range(1, 11)],
            width=80,
        )
        self._concurrency_spin.grid(row=4, column=1, sticky="w")
        self._concurrency_spin.configure(state="disabled")
        ctk.CTkLabel(inputs, text="(max in-flight)", font=ctk.CTkFont(size=11), text_color=("gray50", "gray50")).grid(
            row=4, column=2, sticky="w", padx=(4, 0)
        )

        # ── Log panel ──────────────────────────────────────────────────────────
        ctk.CTkLabel(self, text="Progress", font=ctk.CTkFont(weight="bold"), anchor="w").grid(
            row=3, column=0, sticky="w", padx=16, pady=(8, 2)
        )
        self._log_box = ctk.CTkTextbox(self, state="disabled", font=ctk.CTkFont(family="Consolas", size=11))
        self._log_box.grid(row=4, column=0, sticky="nsew", padx=16, pady=(0, 8))

        # ── Progress bar + status ──────────────────────────────────────────────
        self._progress = ctk.CTkProgressBar(self)
        self._progress.set(0)
        self._progress.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._status_var = ctk.StringVar(value="Ready.")
        ctk.CTkLabel(self, textvariable=self._status_var, anchor="w").grid(
            row=6, column=0, sticky="w", padx=16, pady=(0, 4)
        )

        # ── Action buttons ─────────────────────────────────────────────────────
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=7, column=0, sticky="ew", padx=16, pady=(0, 12))
        btn_row.grid_columnconfigure(1, weight=1)

        self._parse_btn = ctk.CTkButton(
            btn_row, text="Parse Recipes", width=160,
            fg_color="#27ae60", hover_color="#1e8449",
            command=self._start_parse,
        )
        self._parse_btn.grid(row=0, column=0, padx=(0, 8))

        self._open_btn = ctk.CTkButton(
            btn_row, text="Open Output Folder", width=160,
            command=self._open_output, state="disabled",
        )
        self._open_btn.grid(row=0, column=2)

    # ── Event handlers ─────────────────────────────────────────────────────────

    def _on_units_change(self, label: str):
        self._units_var.set(self._label_to_unit[label])

    def _on_free_tier_change(self):
        if self._free_tier_var.get():
            self._concurrency_var.set("1")
            self._concurrency_spin.configure(state="disabled")
        else:
            self._concurrency_spin.configure(state="normal")

    def _browse_epub(self):
        path = filedialog.askopenfilename(
            title="Select EPUB file or Calibre folder",
            filetypes=[("EPUB files", "*.epub"), ("All files", "*.*")],
        )
        if path:
            self._epub_var.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self._output_var.set(path)

    def _open_output(self):
        folder = self._output_var.get()
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.run(["open", folder])
        else:
            subprocess.run(["xdg-open", folder])

    # ── Parse pipeline ─────────────────────────────────────────────────────────

    def _start_parse(self):
        epub_raw = self._epub_var.get().strip()
        output = self._output_var.get().strip()
        api_key = self._api_frame.get_key()

        if not epub_raw:
            messagebox.showwarning("Missing Input", "Please select an EPUB file.")
            return
        if not api_key:
            messagebox.showwarning("API Key", "Please enter your Google API key.")
            return

        # Resolve EPUB path (same logic as CLI)
        from recipeparser.__main__ import _resolve_epub
        try:
            epub_path = _resolve_epub(epub_raw)
        except SystemExit:
            messagebox.showerror("Invalid path", f"Could not find an EPUB at:\n{epub_raw}")
            return

        Path(output).mkdir(parents=True, exist_ok=True)

        self._log_clear()
        self._progress.set(0)
        self._status_var.set("Starting…")
        self._parse_btn.configure(state="disabled")
        self._open_btn.configure(state="disabled")
        self._running = True

        # Attach queue handler to root logger for this run
        handler = _QueueHandler(self._log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        root_log = logging.getLogger()
        root_log.addHandler(handler)
        root_log.setLevel(logging.INFO)

        units = self._label_to_unit.get(self._units_var.get(), self._units_var.get())
        rpm_val, concurrency_val = _parse_run_config(
            self._free_tier_var.get(), self._concurrency_var.get()
        )

        def _run():
            result_path = None
            try:
                os.environ["GOOGLE_API_KEY"] = api_key
                from google import genai
                from recipeparser.pipeline import process_epub as _pipeline
                client = genai.Client(api_key=api_key)
                result_path = _pipeline(
                    epub_path, output, client,
                    units=units, concurrency=concurrency_val, rpm=rpm_val,
                )
            except Exception as exc:
                self._log_queue.put(f"ERROR: {exc}")
            finally:
                root_log.removeHandler(handler)
                self.after(0, self._on_parse_done, result_path)

        threading.Thread(target=_run, daemon=True).start()

    def _on_parse_done(self, result_path: Optional[str]):
        self._running = False
        self._parse_btn.configure(state="normal")
        self._progress.set(1)
        if result_path:
            name = Path(result_path).name
            self._status_var.set(f"Done — {name}")
            self._open_btn.configure(state="normal")
        else:
            self._status_var.set("Finished with errors — check log.")

    # ── Log helpers ────────────────────────────────────────────────────────────

    def _log_clear(self):
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")

    def _log_append(self, text: str):
        self._log_box.configure(state="normal")
        self._log_box.insert("end", text + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _poll_log(self):
        """Drain the log queue and update the text widget; reschedule every 100 ms."""
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._log_append(msg)
                # cheap progress animation while running
                if self._running:
                    current = self._progress.get()
                    self._progress.set(min(current + 0.005, 0.95))
        except queue.Empty:
            pass
        self.after(100, self._poll_log)


# ──────────────────────────────────────────────────────────────────────────────
# Main application window
# ──────────────────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.geometry("860x680")
        self.minsize(720, 540)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        tabs = ctk.CTkTabview(self)
        tabs.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        tabs.add("Parse")
        tabs.add("Categories")

        parse_frame = ParseFrame(tabs.tab("Parse"))
        parse_frame.pack(fill="both", expand=True)

        cat_frame = CategoryEditorFrame(tabs.tab("Categories"))
        cat_frame.pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", lambda: self._on_close(cat_frame))

    def _on_close(self, cat_frame: CategoryEditorFrame):
        if cat_frame.has_unsaved_changes():
            if not messagebox.askyesno(
                "Unsaved changes",
                "You have unsaved category changes. Exit anyway?",
            ):
                return
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_gui():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run_gui()
