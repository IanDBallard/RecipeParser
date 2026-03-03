# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for RecipeParser GUI
# Build: pyinstaller recipeparser.spec
#
# Output: dist\RecipeParser\RecipeParser.exe  (directory mode — fast launch)

import os, importlib.util
from PyInstaller.utils.hooks import collect_data_files, collect_all

# ── Data files ──────────────────────────────────────────────────────────────
# CustomTkinter bundles fonts, images, and theme JSON files that must travel
# with the binary.  collect_all() captures:
#   • datas        — theme JSON, font files, image assets
#   • binaries     — any native extensions (none currently, but future-proof)
#   • hiddenimports — sub-modules that static analysis misses
#
# darkdetect is a hard runtime dependency of customtkinter (used to detect
# the OS light/dark mode) and must be explicitly collected.
datas = []
_d, _b, _h = collect_all("customtkinter")
datas += _d
ctk_binaries = _b
hiddenimports_ctk = _h

# Explicit fallback: if collect_all missed the customtkinter package directory,
# add it directly so theme JSON files and fonts are always bundled.
_ctk_spec = importlib.util.find_spec("customtkinter")
if _ctk_spec and _ctk_spec.submodule_search_locations:
    _ctk_dir = list(_ctk_spec.submodule_search_locations)[0]
    # Only add if collect_all didn't already get it (avoid duplicates)
    _already = any("customtkinter" in str(d[0]) for d in datas)
    if not _already:
        datas += [(_ctk_dir, "customtkinter")]

# darkdetect — required by customtkinter at runtime for theme detection
_d, _b, _h = collect_all("darkdetect")
datas    += _d
ctk_binaries += _b
hiddenimports_ctk += _h

# Explicit fallback for darkdetect
_dd_spec = importlib.util.find_spec("darkdetect")
if _dd_spec and _dd_spec.origin:
    _dd_dir = os.path.dirname(_dd_spec.origin)
    _already = any("darkdetect" in str(d[0]) for d in datas)
    if not _already:
        datas += [(_dd_dir, "darkdetect")]

# The bundled category taxonomy
datas += [("recipeparser/categories.yaml", "recipeparser")]

# ── Binaries + hidden imports from packages with native extensions ───────────
binaries = ctk_binaries
hiddenimports = [
    # GUI — must be explicit; PlatformIO/env may not expose these to PyInstaller
    "customtkinter",
    "tkinter",
    # Standard library / lightweight deps occasionally missed by the analyser
    "sqlite3",
    "recipeparser.paprika_db",
    "yaml",
    "dotenv",
    "pydantic",
    "pydantic.v1",
    "pydantic_core",
    "packaging",
    "email.mime.text",
    "email.mime.multipart",
    # ebooklib
    "ebooklib",
    "ebooklib.epub",
    # lxml is used by ebooklib / beautifulsoup4
    "lxml",
    "lxml.etree",
    "lxml._elementpath",
    "lxml.html",
    # google-genai internals
    "google.genai",
    "google.genai.types",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.auth.transport.grpc",
    "google.oauth2",
]

# Merge customtkinter hiddenimports
hiddenimports += hiddenimports_ctk

# grpc: native DLLs that PyInstaller's static analysis misses
for _pkg in ["grpc", "google.api_core", "google.protobuf"]:
    _d, _b, _h = collect_all(_pkg)
    datas      += _d
    binaries   += _b
    hiddenimports += _h

# lxml: native extensions
_d, _b, _h = collect_all("lxml")
datas      += _d
binaries   += _b
hiddenimports += _h

# ── Analysis ─────────────────────────────────────────────────────────────────
a = Analysis(
    ["recipeparser/gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Test / dev-only packages — keep the bundle lean
        "pytest",
        "unittest",
        "tkinter.test",
        "IPython",
        "jupyter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # directory mode (not --onefile)
    name="RecipeParser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                    # compress executables where UPX is available
    console=False,               # no terminal window — GUI only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="assets/recipeparser.ico",  # uncomment when an icon is available
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="RecipeParser",         # output folder: dist\RecipeParser\
)
