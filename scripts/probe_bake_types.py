"""
Read-only: how well the bake type rules classify a fixed set of the owner's recipes, and how stable the
answer is from one call to the next (Cayenne D29, docs/superpowers/specs/2026-10-14-pan-families-savoury-pies-design.md).

The cases (``tests/goldens/bake_types/cases.csv``: id, title, expected; an empty expected means no bake
type) were chosen at the boundaries the rules decide: savoury pies against casseroles and bakes, creamed
cakes called sponges, yeast cakes, crumbles, cookies and meringues. Each recipe goes through the pans
backfill's own planning (``backfill_pans.plan_row`` with ``force``), so the answer is the one a backfill
would write after the checks. Nothing is written.

    python scripts/probe_bake_types.py --runs 2

Requires SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GOOGLE_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

Case = Tuple[str, str, Optional[str]]  # id, title, expected (None: no bake type)
CASES = ROOT / "tests" / "goldens" / "bake_types" / "cases.csv"
NOT_READ = "(not read)"


def read_cases(path: Path) -> List[Case]:
    with path.open(encoding="utf-8", newline="") as f:
        return [(r["id"], r["title"], r["expected"] or None) for r in csv.DictReader(f)]


def score(cases: Sequence[Case], runs: Sequence[Dict[str, Optional[str]]]) -> Dict[str, Any]:
    """Matches per run, how many recipes got the same answer in every run, and every miss with its answers."""
    matches = [sum(1 for cid, _, want in cases if cid in run and run[cid] == want) for run in runs]
    agree = 0
    misses = []
    for cid, title, want in cases:
        answers = [run[cid] if cid in run else NOT_READ for run in runs]
        if len(set(answers)) == 1 and NOT_READ not in answers:
            agree += 1
        if any(a != want for a in answers):
            misses.append({"id": cid, "title": title, "expected": want, "answers": answers})
    return {"matches": matches, "agree": agree, "misses": misses}


def _load_backfill() -> Any:
    spec = importlib.util.spec_from_file_location("backfill_pans", ROOT / "scripts" / "backfill_pans.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_pans"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    ap = argparse.ArgumentParser(description="Classify the bake type cases and report accuracy and stability.")
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument("--runs", type=int, default=2, help="Times each recipe is asked (default 2).")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    bp = _load_backfill()
    from supabase import create_client  # noqa: PLC0415
    from recipeparser import client as gemini_client  # noqa: PLC0415
    from recipeparser.core.rate_limiter import GlobalRateLimiter  # noqa: PLC0415
    from recipeparser.gemini import tag_pans  # noqa: PLC0415
    if gemini_client is None:
        raise SystemExit("GOOGLE_API_KEY must be set.")
    limiter = GlobalRateLimiter()

    def ask(steps: List[str], ingredients: List[str], system: Optional[str]) -> Any:
        return tag_pans(steps, ingredients, system, gemini_client, limiter=limiter)

    cases = read_cases(args.cases)
    sb = create_client(*bp._creds())
    rows: Dict[str, Dict[str, Any]] = {}
    ids = [c[0] for c in cases]
    for i in range(0, len(ids), 100):
        for row in sb.table("recipes").select(bp.COLUMNS).in_("id", ids[i:i + 100]).execute().data or []:
            rows[row["id"]] = row

    def classify(row: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        result = bp.plan_row(row, ask, force=True)
        return row["id"], result.bake_type if result.status in ("tagged", "none-found") else NOT_READ

    runs: List[Dict[str, Optional[str]]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for _ in range(args.runs):
            runs.append(dict(pool.map(classify, rows.values())))

    s = score(cases, runs)
    n = len(cases)
    print(f"{n} cases, {args.runs} run(s); {len(rows)} read")
    for k, m in enumerate(s["matches"], start=1):
        print(f"  run {k}: {m}/{n} as expected")
    print(f"  same answer in every run: {s['agree']}/{n}")
    for m in s["misses"]:
        answers = ", ".join(a or "none" for a in m["answers"])
        print(f"  miss: {m['title']} - expected {m['expected'] or 'none'}, got {answers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
