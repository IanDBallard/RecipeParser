"""
Read-only: how well the yield kind rules classify a fixed set of the owner's recipes, and how stable the
answer is from one call to the next (Cayenne yield kinds 11.3,
docs/superpowers/specs/2026-11-03-yield-kinds-scaling-design.md).

The cases (``tests/goldens/yield_kinds/cases.csv``: id, title, expected_kind, expected_top, expected_pastry,
expected_count, expected_size; a blank top, pastry, count or size is not scored; the size is each piece's
weight in grams, else its diameter in millimetres) were chosen at the boundaries the kind rules decide: a crust against a
filled pie, a pie in a tin against a free-form one, a pan pizza against a topped round, a loaf in a tin
against a boule, and the plain servings recipes. Each recipe is asked through ``tag_yield`` with its stored
structured ingredients, steps, tins and bake type, then checked by ``check_yield``, exactly as the yields
pass would. Nothing is written: this script has no write path.

    python scripts/probe_yield_kinds.py --runs 2

Requires SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GOOGLE_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# kind, container.top, container.pastry, count, size (weight_g, else diameter_mm)
Answer = Tuple[Optional[str], Optional[str], Optional[str], Optional[float], Optional[float]]
# id, title, kind, top, pastry, count, size (None: not scored)
Case = Tuple[str, str, str, Optional[str], Optional[str], Optional[float], Optional[float]]
CASES = ROOT / "tests" / "goldens" / "yield_kinds" / "cases.csv"
NOT_READ = "(not read)"
COLUMNS = (
    "id,title,structured_ingredients,tokenized_directions,body_rev,derived_rev,pans,bake_type,"
    "servings_min,servings_max,servings_note"
)


def _number(cell: Optional[str]) -> Optional[float]:
    cell = (cell or "").strip()
    return float(cell) if cell else None


def read_cases(path: Path) -> List[Case]:
    with path.open(encoding="utf-8", newline="") as f:
        return [
            (
                r["id"], r["title"], r["expected_kind"], r["expected_top"] or None, r["expected_pastry"] or None,
                _number(r.get("expected_count")), _number(r.get("expected_size")),
            )
            for r in csv.DictReader(f)
        ]


def answer_of(spec: Optional[Dict[str, Any]]) -> Answer:
    """The scored parts of a checked yield_spec."""
    if not spec:
        return None, None, None, None, None
    container = spec.get("container") or {}
    group = spec.get("pieces") or spec.get("area") or {}
    size = group.get("weight_g") if group.get("weight_g") is not None else group.get("diameter_mm")
    return spec.get("kind"), container.get("top"), container.get("pastry"), group.get("count"), size


def _matches(case: Case, answer: Answer) -> bool:
    _, _, kind, top, pastry, count, size = case
    return (
        answer[0] == kind and (top is None or answer[1] == top) and (pastry is None or answer[2] == pastry)
        and (count is None or answer[3] == count) and (size is None or answer[4] == size)
    )


def score(cases: Sequence[Case], runs: Sequence[Dict[str, Answer]]) -> Dict[str, Any]:
    """Matches per run, how many recipes got the same answer in every run, and every miss with its answers."""
    matches = [sum(1 for c in cases if c[0] in run and _matches(c, run[c[0]])) for run in runs]
    agree = 0
    misses = []
    for c in cases:
        answers = [run[c[0]] if c[0] in run else NOT_READ for run in runs]
        if len(set(answers)) == 1 and NOT_READ not in answers:
            agree += 1
        if any(a == NOT_READ or not _matches(c, a) for a in answers):
            misses.append({"id": c[0], "title": c[1], "expected": c[2:], "answers": answers})
    return {"matches": matches, "agree": agree, "misses": misses}


def _creds() -> Tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.")
    return url, key


def _load_backfill() -> Any:
    spec = importlib.util.spec_from_file_location("backfill_pans", ROOT / "scripts" / "backfill_pans.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_pans"] = module
    spec.loader.exec_module(module)
    return module


def _describe(a: Any) -> str:
    return a if isinstance(a, str) else "/".join("-" if x is None else f"{x:g}" if isinstance(x, float) else str(x) for x in a)


def main() -> int:
    ap = argparse.ArgumentParser(description="Classify the yield kind cases and report accuracy and stability.")
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument("--runs", type=int, default=2, help="Times each recipe is asked (default 2).")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    bp = _load_backfill()
    from recipeparser import client as gemini_client  # noqa: PLC0415
    from recipeparser.core.fat_tokens import strip_fat_tokens  # noqa: PLC0415
    from recipeparser.core.rate_limiter import GlobalRateLimiter  # noqa: PLC0415
    from recipeparser.core.yields import check_yield, yield_text  # noqa: PLC0415
    from recipeparser.gemini import tag_yield  # noqa: PLC0415
    from recipeparser.models import RecipePan, StructuredIngredient  # noqa: PLC0415
    from supabase import create_client  # noqa: PLC0415
    if gemini_client is None:
        raise SystemExit("GOOGLE_API_KEY must be set.")
    limiter = GlobalRateLimiter()

    cases = read_cases(args.cases)
    sb = create_client(*_creds())
    rows: Dict[str, Dict[str, Any]] = {}
    ids = [c[0] for c in cases]
    for i in range(0, len(ids), 100):
        for row in sb.table("recipes").select(COLUMNS).in_("id", ids[i:i + 100]).execute().data or []:
            rows[row["id"]] = row

    def classify(row: Dict[str, Any]) -> Tuple[str, Any]:
        try:
            directions = row.get("tokenized_directions") or []
            steps = [strip_fat_tokens(bp._unwrap_pan_tokens(d.get("text") or "")) for d in directions]
            ingredients = [StructuredIngredient.model_validate(x) for x in row.get("structured_ingredients") or []]
            pans = [RecipePan.model_validate(p) for p in row.get("pans") or []]
            answer = tag_yield(
                steps, ingredients, pans, row.get("bake_type"), gemini_client, limiter=limiter,
                yield_text=yield_text(row.get("servings_min"), row.get("servings_max"), row.get("servings_note")),
            )
            spec, _ = check_yield(answer, ingredients, pans)
            return row["id"], answer_of(spec)
        except Exception as exc:  # noqa: BLE001
            print(f"  could not ask about {row.get('title')}: {exc}")
            return row["id"], NOT_READ

    runs: List[Dict[str, Any]] = []
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
        want = _describe(tuple(m["expected"]))
        got = ", ".join(_describe(a) for a in m["answers"])
        print(f"  miss: {m['title']} - expected {want} (kind/top/pastry/count/size), got {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
