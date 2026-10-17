"""
recipeparser/core/tin_servings.py — how many servings a bake makes, from the tins its recipe names
(spec "Estimating servings"). Published serving footprints per tin; first matching rule wins.

Pure: no I/O. HINT_TINS repeats tests/fixtures/hint_tins.json, which is the contract Cayenne's
drift test reads against domain/pans.ts; test_hint_tins_match_fixture keeps the two equal.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from recipeparser.models import RecipePan

HintTin = Dict[str, Any]

# The table is data: one tin per line, so a line may run long.
# ruff: noqa: E501
HINT_TINS: Dict[str, HintTin] = {
    "uk-loaf-1lb": {"family": "loaf", "shape": "loaf", "dims_mm": {"l": 200, "w": 100, "depth": 60}, "capacity_ml": {"min": 750, "max": 1000}},
    "uk-loaf-2lb": {"family": "loaf", "shape": "loaf", "dims_mm": {"l": 230, "w": 130, "depth": 70}, "capacity_ml": {"min": 1100, "max": 1600}},
    "us-bundt-6": {"family": "ring", "shape": "ring", "dims_mm": {"d": 200, "depth": 80}, "capacity_ml": {"min": 1420, "max": 1420}},
    "us-bundt-10": {"family": "ring", "shape": "ring", "dims_mm": {"d": 241, "depth": 91}, "capacity_ml": {"min": 2370, "max": 2370}},
    "us-bundt-12": {"family": "ring", "shape": "ring", "dims_mm": {"d": 254, "depth": 89}, "capacity_ml": {"min": 2840, "max": 2840}},
    "us-tube-10": {"family": "ring", "shape": "ring", "dims_mm": {"d": 254, "depth": 102}, "capacity_ml": {"min": 3790, "max": 3790}},
    "us-jellyroll": {"family": "sheet", "shape": "sheet", "dims_mm": {"l": 381, "w": 254, "depth": 25}},
    "us-quarter-sheet": {"family": "sheet", "shape": "sheet", "dims_mm": {"l": 307, "w": 224, "depth": 28}},
    "us-half-sheet": {"family": "sheet", "shape": "sheet", "dims_mm": {"l": 419, "w": 289, "depth": 25}},
    "us-rect-13x9": {"family": "traybake", "shape": "rect", "dims_mm": {"l": 330, "w": 229, "depth": 51}},
    "uk-traybake-30x23": {"family": "traybake", "shape": "rect", "dims_mm": {"l": 300, "w": 230, "depth": 40}},
    "uk-swissroll-33x23": {"family": "sheet", "shape": "sheet", "dims_mm": {"l": 330, "w": 230, "depth": 20}},
    "eu-blech-40x30": {"family": "sheet", "shape": "sheet", "dims_mm": {"l": 400, "w": 300, "depth": 25}},
    "eu-gugelhupf-22": {"family": "ring", "shape": "ring", "dims_mm": {"d": 220, "depth": 100}, "capacity_ml": {"min": 2500, "max": 2500}},
    "au-lamington-30x20": {"family": "traybake", "shape": "rect", "dims_mm": {"l": 300, "w": 200, "depth": 30}},
    "au-slab-30x20": {"family": "traybake", "shape": "rect", "dims_mm": {"l": 300, "w": 200, "depth": 45}},
    "au-bar-25x8": {"family": "loaf", "shape": "loaf", "dims_mm": {"l": 250, "w": 80, "depth": 70}, "capacity_ml": {"min": 1400, "max": 1400}},
}

# Serving footprints: cm^2 per serving, cm per loaf serving, ml per ring serving.
_LAYER_CAKE_CM2 = 38.0
_SINGLE_LAYER_CM2 = 55.0
_BAR_CM2 = 31.0
_CHEESECAKE_CM2 = 35.0
_TART_CM2 = 50.0
_LOAF_CM = 2.2
_RING_ML = 240.0
_DEEP_MM = 75.0
_CAKE_GROUP = ("round-cake", "square", "traybake", "sheet")
# Only these stack as layers; a traybake or sheet is one layer however many tins (M2).
_LAYER_FAMILIES = ("round-cake", "square")


def _area_cm2(dims: Dict[str, Any]) -> Optional[float]:
    d, w, l = dims.get("d"), dims.get("w"), dims.get("l")  # noqa: E741
    if d:
        return math.pi * (d / 20) ** 2
    if l and w:
        return (l / 10) * (w / 10)
    return None


def _has_geometry(dims: Any) -> bool:
    return bool(dims.d or (dims.l and dims.w))


def _positive(x: Optional[float]) -> bool:
    return x is not None and math.isfinite(x) and x > 0


def _sound(p: RecipePan) -> bool:
    """A tin the table can use: counted at least once, every stated size and capacity a real positive number."""
    dims = (p.dims_mm.d, p.dims_mm.w, p.dims_mm.l, p.dims_mm.depth)
    return p.count >= 1 and all(v is None or _positive(v) for v in (*dims, p.capacity_ml))


def _resolve(p: RecipePan) -> Tuple[Optional[str], Dict[str, Any], Optional[float]]:
    """(family, dims, capacity_ml) for a tin: its own words first, the named hint filling the gaps."""
    hint = HINT_TINS.get(p.catalog_hint) if p.catalog_hint else None
    dims: Dict[str, Any] = {}
    if _has_geometry(p.dims_mm):
        dims = {k: getattr(p.dims_mm, k) for k in ("d", "w", "l", "depth")}
    elif hint:
        dims = dict(hint["dims_mm"])
    capacity = p.capacity_ml
    if capacity is None and hint and "capacity_ml" in hint:
        cap = hint["capacity_ml"]
        capacity = (cap["min"] + cap["max"]) / 2
    return p.family or (hint["family"] if hint else None), dims, capacity


def _score(x: float) -> Optional[int]:
    return max(1, round(x)) if math.isfinite(x) and x > 0 else None


def estimate_from_tins(pans: Sequence[RecipePan], bake_type: Optional[str]) -> Optional[int]:
    if bake_type == "pizza":
        return None
    tins: List[Tuple[RecipePan, Optional[str], Dict[str, Any], Optional[float]]] = []
    for p in pans:
        if p.removed or not _sound(p):
            continue
        family, dims, capacity = _resolve(p)
        if dims or capacity is not None:
            tins.append((p, family, dims, capacity))
    if not tins:
        return None
    families = {f for _, f, _, _ in tins}
    if None in families or "stovetop" in families or "pizza" in families:
        return None
    kinds = {"cake" if f in _CAKE_GROUP else f for f in families}
    if len(kinds) != 1:
        return None
    kind = kinds.pop()

    if kind == "loaf":
        total = sum(p.count * ((dims.get("l") or dims.get("d") or 0) / 10) / _LOAF_CM for p, _, dims, _ in tins)
        return _score(total)
    if kind == "ring":
        total = sum(p.count * cap / _RING_ML for p, _, _, cap in tins if cap)
        return _score(total)

    areas = [(p.count, a, dims) for p, _, dims, _ in tins if (a := _area_cm2(dims)) is not None]
    if not areas:
        return None
    total_area = sum(n * a for n, a, _ in areas)
    if kind == "tart":
        return _score(total_area / _TART_CM2)
    if bake_type == "bar":
        return _score(total_area / _BAR_CM2)
    if bake_type == "cheesecake":
        return _score(total_area / _CHEESECAKE_CM2)
    sizes = {round(a, 3) for _, a, _ in areas}
    count = sum(n for n, _, _ in areas)
    depth = areas[0][2].get("depth") or 0
    layered = families <= set(_LAYER_FAMILIES)
    if layered and len(sizes) == 1 and (count >= 2 or depth >= _DEEP_MM):
        return _score(areas[0][1] / _LAYER_CAKE_CM2)
    return _score(total_area / _SINGLE_LAYER_CM2)
