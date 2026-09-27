"""
One-off: re-measure SAME_RECIPE and DIFFERENT_TAKE for Cayenne's "Similar in your library"
section (cayenne-web/src/lib/domain/similarity.ts; design
docs/superpowers/specs/2026-09-27-similar-recipes-design.md in the Cayenne repository).

Every recipe carries one embedding of its title + ingredient lines. This script scores every pair
in the library -- a plain dot product of unit vectors, exactly as Cayenne's cosine.ts does -- and
prints three things to read the thresholds from:

  1. the pairs by cosine band, split by whether the two titles match (a same-title pair is
     almost always a copy or the same dish; a different-title pair is what a threshold must not
     sweep up by accident);
  2. for each candidate floor, how many recipe pages would show the section, and the most
     similar recipes any one page would list;
  3. the highest-scoring different-title pairs, to eyeball where copies end and siblings begin.

The thresholds EXPIRE with a different embedding model, a different output_dimensionality, or a
change to the embedded text. Run this after any of those and update similarity.ts.

    python scripts/measure_similarity_tiers.py
    python scripts/measure_similarity_tiers.py --top 100

READ-ONLY: this script only ever selects from `recipes`. It never writes.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (read from the environment, or from .env).
Requires numpy (the `dev` extras): 1,601 recipes are 1.28 million pairs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, List, Optional, Tuple

import numpy as np

EMBEDDING_DIM = 1536
PAGE = 500

BANDS: List[Tuple[float, float]] = [
    (0.99, 1.01),
    (0.97, 0.99),
    (0.95, 0.97),
    (0.93, 0.95),
    (0.91, 0.93),
    (0.89, 0.91),
    (0.87, 0.89),
    (0.85, 0.87),
    (0.80, 0.85),
]
FLOORS: Tuple[float, ...] = (0.99, 0.97, 0.95, 0.93, 0.91, 0.89, 0.87)


def normalise_title(title: Optional[str]) -> str:
    """Case and punctuation folded, so "Plate-Pie!" and "plate pie" are one title."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def unit_matrix(vectors: List[List[float]]) -> np.ndarray:
    """One row per vector, each divided by its L2 norm; a zero vector stays zero (cosine.ts normalise)."""
    m = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return np.divide(m, norms, out=np.zeros_like(m), where=norms > 0)


def _pairs(n: int) -> Tuple[np.ndarray, np.ndarray]:
    return np.triu_indices(n, 1)


def band_table(sims: np.ndarray, titles: List[str]) -> List[Tuple[float, float, int, int]]:
    """(low, high, same-title pairs, different-title pairs) per band; each pair counted once."""
    i, j = _pairs(len(titles))
    scores = sims[i, j]
    same = np.array([titles[a] == titles[b] for a, b in zip(i, j)], dtype=bool)
    table = []
    for lo, hi in BANDS:
        in_band = (scores >= lo) & (scores < hi)
        table.append((lo, hi, int((in_band & same).sum()), int((in_band & ~same).sum())))
    return table


def pages_with_neighbour(sims: np.ndarray, floor: float) -> Tuple[int, int]:
    """(recipe pages with at least one other recipe at or above floor, the most any page lists)."""
    s = sims.copy()
    np.fill_diagonal(s, -1.0)
    counts = (s >= floor).sum(axis=1)
    return int((counts > 0).sum()), int(counts.max()) if len(counts) else 0


def top_different_title_pairs(sims: np.ndarray, titles: List[str], n: int) -> List[Tuple[float, int, int]]:
    """The n best-scoring pairs whose titles differ, best first, as (score, i, j) with i < j."""
    i, j = _pairs(len(titles))
    scores = sims[i, j]
    out: List[Tuple[float, int, int]] = []
    for k in np.argsort(-scores):
        a, b = int(i[k]), int(j[k])
        if titles[a] == titles[b]:
            continue
        out.append((float(scores[k]), a, b))
        if len(out) >= n:
            break
    return out


def _parse_embedding(raw: Any) -> Optional[List[float]]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, list) or len(value) != EMBEDDING_DIM:
        return None
    return [float(x) for x in value]


def load_rows(sb: Any) -> Tuple[List[dict], int]:
    """Every recipe with a valid embedding, and how many were skipped as malformed."""
    rows: List[dict] = []
    skipped = 0
    offset = 0
    while True:
        page = (
            sb.table("recipes")
            .select("id,title,source_key,embedding")
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
            .data
            or []
        )
        if not page:
            break
        for row in page:
            vec = _parse_embedding(row.get("embedding"))
            if vec is None:
                skipped += 1
                continue
            rows.append(
                {"id": row["id"], "title": row.get("title") or "", "source_key": row.get("source_key"), "vec": vec}
            )
        offset += PAGE
    return rows, skipped


def _label(row: dict) -> str:
    return f"{row['title'][:45]:45} [{(row['source_key'] or '-')[:18]}]"


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-measure Cayenne's similar-recipe tiers. READ-ONLY.")
    ap.add_argument("--top", type=int, default=70, help="Different-title pairs to list (default: 70).")
    args = ap.parse_args()

    from dotenv import load_dotenv  # noqa: PLC0415
    from supabase import create_client  # noqa: PLC0415

    load_dotenv()
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    print("Reading id, title, source_key, embedding from `recipes` (read-only)...")
    rows, skipped = load_rows(sb)
    n = len(rows)
    print(f"  valid: {n}   malformed/skipped: {skipped}   pairs: {n * (n - 1) // 2}")
    if n < 2:
        print("Fewer than two valid embeddings -- nothing to measure.")
        return 2

    m = unit_matrix([r["vec"] for r in rows])
    sims = m @ m.T
    titles = [normalise_title(r["title"]) for r in rows]

    print("\nband          same_title  diff_title")
    for lo, hi, same, diff in band_table(sims, titles):
        print(f"{lo:.2f}-{min(hi, 1.0):.2f}   {same:>10}  {diff:>10}")
    i, j = _pairs(n)
    all_scores = sims[i, j]
    percentiles = {p: round(float(np.percentile(all_scores, p)), 4) for p in (50, 90, 99, 99.9, 99.99)}
    print("percentiles, all pairs:", percentiles)

    print("\nfloor   pages_with_>=1   max_listed")
    for floor in FLOORS:
        pages, most = pages_with_neighbour(sims, floor)
        print(f"{floor:.2f}    {pages:>14}   {most:>10}")

    print(f"\ntop {args.top} different-title pairs")
    for score, a, b in top_different_title_pairs(sims, titles, args.top):
        print(f"{score:.4f} | {_label(rows[a])} | {_label(rows[b])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
