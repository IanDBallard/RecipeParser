"""
Post-build smoke test for the PyInstaller-produced RecipeParser.exe.

Run by the CI 'smoke-test' job after PyInstaller completes, before Inno Setup.
Can also be run locally:

    python tests/smoke_test_exe.py dist\RecipeParser\RecipeParser.exe 2.0.4

Exit codes:
    0  — all checks passed
    1  — one or more checks failed (details printed to stderr)
"""
import subprocess
import sys
import re
from pathlib import Path


def run(exe: Path, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run the exe with the given args and return the CompletedProcess.

    The exe MUST be run from its own directory so that PyInstaller's
    directory-mode bundle can find its sibling DLLs and data files.
    """
    return subprocess.run(
        [str(exe.resolve())] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(exe.parent),   # <-- critical: run from the bundle directory
    )


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

    print(f"\n=== Smoke test: {exe} ===\n")
    failures = 0

    # ── 1. Exe exists and is non-empty ────────────────────────────────────────
    exists = exe.exists() and exe.stat().st_size > 0
    if not check("Exe file exists and is non-empty", exists,
                 f"Path: {exe}"):
        failures += 1
        # No point continuing if the exe isn't there
        print(f"\n=== {failures} smoke test(s) FAILED ===\n", file=sys.stderr)
        return failures

    # ── 2. Bundle directory size sanity check (> 20 MB total) ────────────────
    # PyInstaller directory-mode: the .exe itself is small (~5-15 MB);
    # the full bundle (DLLs + data) should be well over 20 MB total.
    bundle_dir = exe.parent
    bundle_mb = dir_size_mb(bundle_dir)
    exe_mb = exe.stat().st_size / (1024 * 1024)
    print(f"  [INFO] Exe size: {exe_mb:.1f} MB  |  Bundle dir total: {bundle_mb:.1f} MB")
    if not check(f"Bundle directory > 20 MB (actual: {bundle_mb:.1f} MB)", bundle_mb > 20,
                 "Bundle appears too small — PyInstaller may have failed silently or "
                 "the artifact download may be incomplete"):
        failures += 1

    # ── 3. --help exits 0 and prints usage ───────────────────────────────────
    try:
        r = run(exe, ["--help"])
        ok = r.returncode == 0 and "epub" in r.stdout.lower()
        if not check("--help exits 0 and mentions 'epub'", ok,
                     f"rc={r.returncode}\nstdout={r.stdout[:300]}\nstderr={r.stderr[:300]}"):
            failures += 1
    except subprocess.TimeoutExpired:
        check("--help completes within timeout", False, "Timed out after 30s")
        failures += 1

    # ── 4. --version exits 0 and prints the expected version ─────────────────
    try:
        r = run(exe, ["--version"])
        # argparse --version writes to stdout (Python 3.4+)
        version_output = (r.stdout + r.stderr).strip()
        version_ok = r.returncode == 0
        if not check("--version exits 0", version_ok,
                     f"rc={r.returncode}\noutput={version_output}"):
            failures += 1

        if expected_version:
            ver_match = expected_version in version_output
            if not check(f"--version output contains '{expected_version}'", ver_match,
                         f"Got: {version_output}"):
                failures += 1
        else:
            # At minimum a semver-like string should appear
            has_ver = bool(re.search(r"\d+\.\d+\.\d+", version_output))
            if not check("--version output contains a version number", has_ver,
                         f"Got: {version_output}"):
                failures += 1
    except subprocess.TimeoutExpired:
        check("--version completes within timeout", False, "Timed out after 30s")
        failures += 1

    # ── 5. Passing a non-existent epub exits non-zero with an error message ──
    try:
        r = run(exe, ["nonexistent_file.epub"])
        error_exit = r.returncode != 0
        has_error_msg = "error" in (r.stdout + r.stderr).lower() or "not found" in (r.stdout + r.stderr).lower()
        if not check("Bad epub path exits non-zero", error_exit,
                     f"rc={r.returncode}"):
            failures += 1
        if not check("Bad epub path prints an error message", has_error_msg,
                     f"stdout={r.stdout[:200]}\nstderr={r.stderr[:200]}"):
            failures += 1
    except subprocess.TimeoutExpired:
        check("Bad epub path check completes within timeout", False, "Timed out after 30s")
        failures += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if failures == 0:
        print(f"=== All smoke tests PASSED ===\n")
    else:
        print(f"=== {failures} smoke test(s) FAILED ===\n", file=sys.stderr)

    return failures


if __name__ == "__main__":
    sys.exit(main())
