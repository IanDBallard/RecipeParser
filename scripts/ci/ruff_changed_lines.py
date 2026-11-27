"""Run ruff on the Python files a branch changed and report only findings on lines it changed.

The repository carries older ruff findings (long lines, import order) that predate the
CI check. Failing a pull request for a finding it did not write would make every edit to
an old file a clean-up first, so this gate holds new and changed lines to the rules and
leaves the old ones to a separate, deliberate clean-up (docs/architecture/ACF_ROADMAP.md,
Phase 5).

Usage: python scripts/ci/ruff_changed_lines.py --base origin/master
Exit status: 0 no finding on a changed line, 1 findings, 2 git or ruff failed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GateError(Exception):
    """git or ruff did not run as expected."""


def _run(args: list[str], *, ok: tuple[int, ...] = (0,)) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode not in ok:
        raise GateError(f"{' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def changed_lines(base: str) -> dict[str, set[int]]:
    """Each changed .py file (repository-relative POSIX path) and the line numbers it added or changed."""
    diff = _run(["git", "diff", "--unified=0", "--diff-filter=AMR", "--no-color", f"{base}...HEAD", "--", "*.py"])
    lines: dict[str, set[int]] = {}
    current: str | None = None
    for row in diff.splitlines():
        if row.startswith("+++ "):
            target = row[4:]
            current = target[2:] if target.startswith("b/") else None
            if current is not None:
                lines.setdefault(current, set())
            continue
        match = _HUNK.match(row)
        if match and current is not None:
            start, count = int(match.group(1)), int(match.group(2) or "1")
            lines[current].update(range(start, start + count))
    return {path: rows for path, rows in lines.items() if rows}


def findings_on(lines: dict[str, set[int]], root: Path) -> list[dict]:
    """ruff's findings in those files that fall on a changed line."""
    if not lines:
        return []
    out = _run(
        [sys.executable, "-m", "ruff", "check", "--force-exclude", "--output-format", "json", *sorted(lines)],
        ok=(0, 1),
    )
    hits = []
    for finding in json.loads(out or "[]"):
        path = Path(finding["filename"]).resolve().relative_to(root).as_posix()
        if finding["location"]["row"] in lines.get(path, set()):
            hits.append({**finding, "filename": path})
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="ref the branch is compared with, e.g. origin/master")
    args = parser.parse_args()
    try:
        root = Path(_run(["git", "rev-parse", "--show-toplevel"]).strip()).resolve()
        hits = findings_on(changed_lines(args.base), root)
    except GateError as error:
        print(f"ruff gate: {error}", file=sys.stderr)
        return 2
    for hit in hits:
        loc = hit["location"]
        print(f"{hit['filename']}:{loc['row']}:{loc['column']}: {hit['code']} {hit['message']}")
    if hits:
        print(f"ruff gate: {len(hits)} finding(s) on changed lines.", file=sys.stderr)
        return 1
    print("ruff gate: no finding on a changed line.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
