"""Regenerate requirements.txt, the lock of pyproject.toml's dependencies, or check it is current.

requirements.txt is what the Docker image, the CI tests and the Windows build install, so every
version the service runs, its dependencies' dependencies included, is written down. It is
generated: edit pyproject.toml, then run this. Versions already in the lock are kept unless
pyproject.toml rules them out or --upgrade names the package.

Usage: python scripts/ci/lock_requirements.py [--check] [--upgrade PACKAGE ...]
Needs uv (pip install uv). Exit status: 0 done or current, 1 the lock is stale, 2 uv failed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "requirements.txt"
COMMAND = "python scripts/ci/lock_requirements.py"
# The oldest Python the lock must install on: the Docker image and CI run 3.11 and 3.12.
PYTHON_FLOOR = "3.11.5"


def compile_lock(output: Path, upgrade: list[str]) -> None:
    """Write the lock to ``output``, keeping the versions ``output`` already holds."""
    command = [
        "uv", "pip", "compile", "pyproject.toml", "--universal", "--python-version", PYTHON_FLOOR,
        "--no-annotate", "--quiet", "--custom-compile-command", COMMAND, "--output-file", str(output),
    ]
    for package in upgrade:
        command += ["--upgrade-package", package]
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate or check requirements.txt.")
    parser.add_argument("--check", action="store_true", help="fail when the lock is stale; write nothing")
    parser.add_argument("--upgrade", nargs="+", default=[], metavar="PACKAGE")
    args = parser.parse_args()
    try:
        if not args.check:
            compile_lock(LOCK, args.upgrade)
            return 0
        with tempfile.TemporaryDirectory() as folder:
            fresh = Path(folder) / LOCK.name
            fresh.write_bytes(LOCK.read_bytes())
            compile_lock(fresh, [])
            current = fresh.read_text(encoding="utf-8") == LOCK.read_text(encoding="utf-8")
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"requirements lock: {error}", file=sys.stderr)
        return 2
    if not current:
        print(f"requirements.txt does not match pyproject.toml; run `{COMMAND}`.", file=sys.stderr)
        return 1
    print("requirements.txt matches pyproject.toml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
