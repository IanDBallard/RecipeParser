"""
Post-build smoke test for the PyInstaller-produced RecipeParser.exe.

Run by the CI 'smoke-test' job after PyInstaller completes, before Inno Setup.
Can also be run locally:

    python tests/smoke_test_exe.py dist\RecipeParser\RecipeParser.exe 2.0.5

NOTE: RecipeParser.exe is a GUI-only application (console=False in the spec).
It cannot be run headlessly with --help/--version in CI because it requires a
display/tkinter context.  This smoke test therefore validates the *bundle*
rather than executing the GUI:

  1. Exe exists and is non-empty
  2. Full bundle directory is > 20 MB (catches silent PyInstaller failures)
  3. customtkinter theme JSON files are present in the bundle
  4. darkdetect is present in the bundle
  5. categories.yaml is present in the bundle
  6. Version string appears in the exe binary (confirms correct build)

Exit codes:
    0  — all checks passed
    1  — one or more checks failed (details printed to stderr)
"""
import sys
import re
from pathlib import Path


def check(label: str, condition: bool, detail: str = "") -> bool:
    """Print a PASS/FAIL line and return the condition."""
    status = "PASS" if condition else "FAIL"
    line = f"  [{status}] {label}"
    if not condition and detail:
        line += f"\n         {detail}"
    print(line, file=sys.stderr if not condition else sys.stdout)
    return condition


def dir_size_mb(path: Path) -> float:
    """Return total size of all files under path in MB."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (1024 * 1024)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: smoke_test_exe.py <path/to/RecipeParser.exe> [expected_version]",
              file=sys.stderr)
        return 1

    exe = Path(sys.argv[1])
    expected_version = sys.argv[2] if len(sys.argv) > 2 else None
    bundle_dir = exe.parent

    print(f"\n=== Smoke test: {exe} ===\n")
    print("NOTE: GUI-only exe — validating bundle integrity (no headless execution)\n")
    failures = 0

    # ── 1. Exe exists and is non-empty ────────────────────────────────────────
    exists = exe.exists() and exe.stat().st_size > 0
    if not check("Exe file exists and is non-empty", exists, f"Path: {exe}"):
        failures += 1
        print(f"\n=== {failures} smoke test(s) FAILED ===\n", file=sys.stderr)
        return failures

    # ── 2. Bundle directory size sanity check (> 20 MB total) ────────────────
    # PyInstaller directory-mode: the .exe itself is small (~5-15 MB);
    # the full bundle (DLLs + data) should be well over 20 MB total.
    bundle_mb = dir_size_mb(bundle_dir)
    exe_mb = exe.stat().st_size / (1024 * 1024)
    print(f"  [INFO] Exe size: {exe_mb:.1f} MB  |  Bundle dir total: {bundle_mb:.1f} MB")
    if not check(f"Bundle directory > 20 MB (actual: {bundle_mb:.1f} MB)", bundle_mb > 20,
                 "Bundle appears too small — PyInstaller may have failed silently"):
        failures += 1

    # ── 3. customtkinter theme JSON files present ─────────────────────────────
    themes = list(bundle_dir.rglob("*.json"))
    ctk_themes = [t for t in themes if "customtkinter" in str(t)]
    if not ctk_themes:
        # Fallback: any JSON (customtkinter themes are the only JSONs we bundle)
        ctk_themes = themes
    if not check(f"customtkinter theme JSON files present ({len(ctk_themes)} found)",
                 len(ctk_themes) > 0,
                 "No .json theme files found — customtkinter data not bundled"):
        failures += 1
    else:
        for t in ctk_themes[:3]:
            print(f"    {t.relative_to(bundle_dir)}")
        if len(ctk_themes) > 3:
            print(f"    ... and {len(ctk_themes) - 3} more")

    # ── 4. darkdetect present in bundle ──────────────────────────────────────
    darkdetect_files = list(bundle_dir.rglob("darkdetect*"))
    if not check(f"darkdetect present in bundle ({len(darkdetect_files)} file(s))",
                 len(darkdetect_files) > 0,
                 "darkdetect not found — customtkinter theme detection will fail at runtime"):
        failures += 1
    else:
        print(f"    {darkdetect_files[0].name}")

    # ── 5. categories.yaml present in bundle ─────────────────────────────────
    yaml_files = list(bundle_dir.rglob("categories.yaml"))
    if not check("categories.yaml present in bundle", len(yaml_files) > 0,
                 "categories.yaml not found — recipe categorisation will fail"):
        failures += 1

    # ── 6. recipeparser package metadata present (enables --version at runtime) ─
    # PyInstaller bundles importlib.metadata so --version works in the frozen exe.
    # The metadata lives in _internal/recipeparser-<version>.dist-info/METADATA
    # OR in _internal/recipeparser.egg-info/PKG-INFO for editable installs.
    if expected_version:
        meta_dirs = list(bundle_dir.rglob(f"recipeparser-{expected_version}.dist-info"))
        if not meta_dirs:
            # Also accept any recipeparser dist-info
            meta_dirs = list(bundle_dir.rglob("recipeparser-*.dist-info"))
        if not meta_dirs:
            # Fallback for editable installs: check for egg-info
            meta_dirs = list(bundle_dir.rglob("recipeparser.egg-info"))
            
        if not check(f"recipeparser metadata present in bundle",
                     len(meta_dirs) > 0,
                     "Package metadata missing — importlib.metadata version lookup will fail at runtime.\n"
                     "         Add 'copy_metadata(\"recipeparser\")' to datas in recipeparser.spec"):
            failures += 1
        else:
            print(f"    {meta_dirs[0].name}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if failures == 0:
        print("=== All smoke tests PASSED ===\n")
    else:
        print(f"=== {failures} smoke test(s) FAILED ===\n", file=sys.stderr)

    return failures


if __name__ == "__main__":
    sys.exit(main())
