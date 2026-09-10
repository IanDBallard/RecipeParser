"""
One-off: measure MATCH_FLOOR and MATCH_CEILING for the Cayenne "match strength"
bar.

Cayenne ranks search results by cosine similarity over Gemini embeddings, but
text embeddings are anisotropic -- they live in a narrow cone of the 1536-dim
space, so two *unrelated* recipes still score well above zero. Rendering a raw
cosine score as a 0-100% bar would misrepresent an irrelevant hit as a partial
match. Instead the bar shows where a score sits between a measured floor (the
score of an unrelated pair) and ceiling (the score of an unambiguous match):

    fill = clamp((score - MATCH_FLOOR) / (MATCH_CEILING - MATCH_FLOOR), 0, 1)

This script measures those two constants for this model over this corpus:

  FLOOR   = 90th percentile of {score(query, recipe) for every sample query x
            every recipe in the library}. Almost every pair is unrelated, so
            the bulk of that set *is* the noise band -- the percentile keeps
            the rare true match from dragging the floor up.
  CEILING = median of {score(recipe's own title, that recipe's stored
            embedding) for a sample of recipes}. That's the best the index
            can do on this corpus -- what a full bar should mean.

Cosine is computed as a plain dot product of unit vectors, mirroring Cayenne's
cosine.ts normalise() exactly (divide by L2 norm; a zero vector stays zero).

    python scripts/measure_match_band.py
    python scripts/measure_match_band.py --queries 30 --titles 80 --seed 1

READ-ONLY: this script only ever selects from `recipes`. It never writes.

Requires SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and GOOGLE_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.adapters.api import _get_client  # noqa: E402  (existing Gemini client factory)
from recipeparser.config import GEMINI_EMBEDDING_MODEL  # noqa: E402
from recipeparser.gemini import get_embeddings  # noqa: E402

PAGE = 500
EMBEDDING_DIM = 1536

# ~20 plausible library searches: a spread of bare keywords and natural-
# language phrasing, the way a person actually types into a search box.
# Kept as a module constant so the measurement is reproducible and reviewable.
QUERIES: List[str] = [
    "chicken",
    "something quick for a weeknight",
    "chocolate dessert",
    "vegetarian curry",
    "what can I do with leftover rice",
    "bread",
    "soup for a cold day",
    "pasta with anchovies",
    "roast lamb",
    "gluten free baking",
    "fish tacos",
    "easy pasta bake",
    "what to make with courgettes",
    "birthday cake",
    "thai green curry",
    "salad for a picnic",
    "quick breakfast ideas",
    "beef stew",
    "vegan lasagne",
    "dinner party dessert",
]


def _parse_embedding(raw: Any) -> Optional[List[float]]:
    """Parse a `recipes.embedding` value into exactly 1536 finite floats, or
    None if it doesn't qualify. `embedding` is stored as JSON text of a
    1536-number array, but this is defensive about a client that has already
    decoded it to a list."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            values = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    elif isinstance(raw, list):
        values = raw
    else:
        return None
    if not isinstance(values, list) or len(values) != EMBEDDING_DIM:
        return None
    out: List[float] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        out.append(f)
    return out


def normalise(vector: List[float]) -> List[float]:
    """Mirrors Cayenne's cosine.ts normalise(): divide by the L2 norm, and
    return a zero vector (rather than dividing by zero) when the norm is
    zero."""
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return [0.0] * len(vector)
    return [v / norm for v in vector]


def dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def percentile(data: List[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy's default 'linear' method), so
    the 90th percentile of a set is reproducible without a numpy dependency."""
    if not data:
        raise ValueError("percentile() of empty data")
    ordered = sorted(data)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


def load_valid_rows(sb: Any) -> "tuple[int, int, List[Dict[str, Any]]]":
    """Page through `recipes`, parsing/normalising each embedding. Returns
    (rows_read, rows_skipped_malformed, valid_rows)."""
    rows_read = 0
    malformed = 0
    valid_rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (
            sb.table("recipes")
            .select("id,title,embedding")
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
            .data
            or []
        )
        if not page:
            break
        for row in page:
            rows_read += 1
            vec = _parse_embedding(row.get("embedding"))
            if vec is None:
                malformed += 1
                continue
            valid_rows.append(
                {"id": row["id"], "title": row.get("title"), "unit": normalise(vec)}
            )
        offset += PAGE
    return rows_read, malformed, valid_rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure MATCH_FLOOR and MATCH_CEILING for the Cayenne match-strength bar. READ-ONLY."
    )
    ap.add_argument(
        "--queries", type=int, default=20,
        help="Number of sample queries to embed for the floor measurement (default: 20).",
    )
    ap.add_argument(
        "--titles", type=int, default=50,
        help="Number of recipe titles to sample for the ceiling measurement (default: 50).",
    )
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for deterministic sampling (default: 0).")
    ap.add_argument(
        "--percentile", type=float, default=90.0,
        help="Percentile of the floor score set to report as MATCH_FLOOR (default: 90).",
    )
    args = ap.parse_args()

    from supabase import create_client  # noqa: PLC0415
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    print("Reading id, title, embedding from `recipes` (read-only)...")
    rows_read, malformed, valid_rows = load_valid_rows(sb)
    print(f"  rows read: {rows_read}   malformed/skipped: {malformed}   valid: {len(valid_rows)}")

    if not valid_rows:
        print("No valid embeddings found -- cannot measure anything.")
        return 2

    titled_rows = [r for r in valid_rows if r["title"]]

    num_queries = min(args.queries, len(QUERIES))
    if args.queries > len(QUERIES):
        print(
            f"WARNING: --queries {args.queries} exceeds the {len(QUERIES)} available "
            f"sample queries; using {len(QUERIES)}."
        )
    num_titles = min(args.titles, len(titled_rows))
    if args.titles > len(titled_rows):
        print(
            f"WARNING: --titles {args.titles} exceeds the {len(titled_rows)} recipes with "
            f"both a title and a valid embedding; using {num_titles}."
        )

    rng = random.Random(args.seed)
    sample_queries = rng.sample(QUERIES, num_queries)
    sample_recipes = rng.sample(titled_rows, num_titles)

    total_calls = num_queries + num_titles
    print(f"Embedding model: {GEMINI_EMBEDDING_MODEL}")
    print(
        f"About to make {total_calls} Gemini embedding API call(s): "
        f"{num_queries} quer{'y' if num_queries == 1 else 'ies'} + {num_titles} recipe title(s)."
    )

    gemini_client = _get_client()

    # FLOOR: score every sample query against EVERY valid recipe.
    floor_scores: List[float] = []
    calls_made = 0
    for q in sample_queries:
        vec = get_embeddings(q, gemini_client)
        calls_made += 1
        qvec = normalise([float(x) for x in vec])
        for row in valid_rows:
            floor_scores.append(dot(qvec, row["unit"]))

    # CEILING: score each sampled recipe's own title against its stored embedding.
    ceiling_scores: List[float] = []
    for row in sample_recipes:
        vec = get_embeddings(row["title"], gemini_client)
        calls_made += 1
        tvec = normalise([float(x) for x in vec])
        ceiling_scores.append(dot(tvec, row["unit"]))

    match_floor = percentile(floor_scores, args.percentile)
    match_ceiling = statistics.median(ceiling_scores)

    print()
    print("=" * 72)
    print(f"Rows read from `recipes`:        {rows_read}")
    print(f"Rows skipped (malformed):        {malformed}")
    print(f"API calls made:                  {calls_made}")
    print(f"Queries used:                    {num_queries}")
    print(f"Recipe titles used:              {num_titles}")
    print(f"Floor score set size:            {len(floor_scores)}  (queries x valid recipes)")
    print(f"Ceiling score set size:          {len(ceiling_scores)}")
    print("-" * 72)
    print(f"MATCH_FLOOR   ({args.percentile:g}th pct of floor set):   {match_floor:.6f}")
    print(f"MATCH_CEILING (median of ceiling set):    {match_ceiling:.6f}")
    print("-" * 72)
    print(
        "Floor set   min/median/max: "
        f"{min(floor_scores):.6f} / {statistics.median(floor_scores):.6f} / {max(floor_scores):.6f}"
    )
    print(
        "Ceiling set min/median/max: "
        f"{min(ceiling_scores):.6f} / {statistics.median(ceiling_scores):.6f} / {max(ceiling_scores):.6f}"
    )
    print("=" * 72)

    if match_floor >= match_ceiling:
        print()
        print("!" * 72)
        print("!! MATCH_FLOOR >= MATCH_CEILING -- the procedure produced nonsense.")
        print("!! DO NOT record these constants. Investigate before using them.")
        print("!" * 72)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
